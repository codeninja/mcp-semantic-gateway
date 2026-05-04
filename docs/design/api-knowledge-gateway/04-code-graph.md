# 04 — Code Graph

The code-graph module is the new core mechanism this design adds to the
pipeline. It is the answer to "given an unknown method or API interface,
extract the surrounding context that lets an LLM induce real
implementation patterns" (decision N-3).

The Atlas captures *what exists*. The code graph captures *how it is
referenced* — across the corpus we ingested. The pattern-mining module
([05](05-pattern-mining.md)) then slices the code graph around target
entities and feeds those slices into deterministic, statistical, and
LLM-based miners.

## Subjects

A `CodeGraph` is parameterized by **subject** — which corpus the graph
is built over. **All three subjects ship in v0** with bounded scope per
subject (decision N-3).

| Subject | What it covers | v0 scope |
|---|---|---|
| `IngestedCodeGraph` | The source's own corpus: doc tree, embedded examples, repo files when the parser opts in to deep parsing | full per parser |
| `LocalCodeGraph` | The user's local project — how *this* repo references the ingested API | Python AST only at v0; TS to follow |
| `PublicCorpusGraph` | External public code referencing the API | one backend (GitHub Code Search); opt-in per source; license-allowlisted |

All three implement the same `CodeGraph` Protocol. Pattern-mining and
the knowledge surface treat them polymorphically. A `CodeGraphMux`
([01](01-modules-and-contracts.md)) merges enabled subjects' slices
into a `MergedSlice` for the LLM-induced miner and the discriminator.

## "AST" disambiguation

The module name uses *code graph* and not *AST graph* deliberately,
because not every source ships code. The graph composes whatever
structure the parser can supply:

| Source kind | Graph layer |
|---|---|
| `.d.ts` (TS source) | Full TypeScript AST + type-resolver references |
| Sphinx-with-source (Python) | Full Python AST over the source if the parser is configured to fetch it; AST over example blocks otherwise |
| Godot XML | Doc tree + ref graph (`[code]`/`[method]`/`[member]` BBCode tags) + AST over GDScript example blocks |
| OpenAPI | Doc tree + `$ref` graph + AST only over embedded code samples (rare) |
| Sphinx (HTML only) | Doc tree + ref graph + AST over example blocks |
| Markdown | Symbol extraction + reference graph |

In v1, *AST* applies most strongly to embedded code blocks across all
sources. It applies in the strict sense (parsed source files) for
`.d.ts` and for Sphinx sources whose parser opted into deep parsing.

The `Reference` type is uniform across these layers; downstream code
doesn't branch on which layer produced an edge.

## Reference model

```python
class Reference(BaseModel):
    """A typed pointer from one code-graph node to another."""
    subject: CodeGraphSubject        # which corpus produced this reference
    source_entity: str | None        # Atlas entity id (None if reference is from prose with no entity context)
    target_entity: str               # Atlas entity id
    kind: ReferenceKind
    location: SourceLocation         # file path + line range in the corpus
    context: str                     # short surrounding text for evidence
    confidence: float                # 1.0 for AST-extracted, lower for prose-derived
    provenance: Provenance | None    # populated for LOCAL_PROJECT and PUBLIC_CORPUS

class Provenance(BaseModel):
    """Subject-specific origin metadata."""
    # LOCAL_PROJECT: workspace-relative path
    project_path: str | None = None

    # PUBLIC_CORPUS: license + repo + commit
    repo: str | None = None
    commit_sha: str | None = None
    license: str | None = None
    star_count: int | None = None    # rough quality signal

class ReferenceKind(Enum):
    CALL = "call"                    # method/function invocation in code
    INSTANTIATE = "instantiate"      # constructor call
    INHERIT = "inherit"              # extends / implements
    TYPE_USE = "type-use"            # parameter or return type
    DOC_LINK = "doc-link"            # cross-link in docs
    EXAMPLE_USE = "example-use"      # appears in an example for an entity
    OVERRIDE = "override"            # method override
    SIBLING = "sibling"              # appears in same scope (looser; produces co_occurs_with)
    PRECEDES = "precedes"            # appears before in same control-flow block (produces sequenced_with)
```

The `confidence` field matters: deterministic miners should weight
high-confidence references (`CALL`, `INSTANTIATE`, `INHERIT`) above
prose-only links.

## Construction — per subject

Each subject has a distinct build pipeline. All three write into the
same `code_graph_references` table, distinguished by the `subject`
column.

### `IngestedCodeGraph` construction

```python
class IngestedCodeGraphBuilder(Protocol):
    """One per parser content_kind. Built into the parser package."""
    content_kind: str

    async def build(self, snapshot: AtlasSnapshot, raw: RawSnapshot) -> CodeGraph: ...
```

Builders run after `Parser.parse()` produces the `AtlasSnapshot`. They
read both the snapshot (entities + edges) and the raw artifacts (to
re-tokenize example code, extract AST), and emit a graph stored in
SQLite alongside the Atlas.

Construction is deterministic and additive:

1. Walk every `Example` attached to entities; tokenize the example;
   extract symbol references.
2. Resolve symbols against the Atlas (`Parser.extract_symbols` is the
   resolver).
3. For each resolved symbol, emit a `Reference` with the right kind.
4. Within each example, emit `SIBLING` and `PRECEDES` references for
   co-occurring symbols.
5. For prose docs, run `Parser.extract_symbols` over body text and emit
   `DOC_LINK` references at lower confidence.

Cost: dominated by example-block tokenization. No LLM calls.

### `LocalCodeGraph` construction

```python
class LocalCodeGraphBuilder:
    """One per supported language. v0: Python."""
    language: Literal["python"]                # "typescript" follows phase-by-phase

    async def build(
        self,
        snapshot: AtlasSnapshot,
        project_root: Path,
    ) -> CodeGraph: ...
```

Pipeline (Python v0):

1. **Discover.** `LocalProjectAcquirer` walks the project root,
   respecting `.gitignore`. Result is a list of `.py` files.
2. **Parse AST.** Use Python stdlib `ast` to parse each file.
3. **Build import graph.** For each `Import` / `ImportFrom`, record
   the module symbol → resolved-name mapping per file scope.
4. **Resolve calls and types.** For each `Call`, `Attribute`, and
   `Name` node, resolve to a fully-qualified symbol via the
   import-graph mapping.
5. **Match against Atlas.** Compare resolved symbols against entity
   `qualified_name`s in every configured ingested source. Match
   strategies (in confidence order):
     - exact qualified-name match → confidence 1.0
     - tail-match against partition group prefix → 0.7
     - fuzzy alias match (configured per source) → 0.5
6. **Emit references** with `subject = LOCAL_PROJECT` and
   `Provenance.project_path` set.

Reference resolution accuracy is the load-bearing concern. Brittle
cases (dynamic dispatch, runtime-built imports) are accepted as
silent misses in v0; the user's project is best-effort, not a
correctness oracle.

**Privacy.** `LocalCodeGraph` content **never** leaves the host:
- No raw local code is sent to LLMs in any miner that is not flagged
  `local_safe = true`.
- No raw local code is stored in Qdrant payloads — only resolved
  references and entity ids.
- An optional whitelist limits which file globs participate.

### `PublicCorpusGraph` construction

```python
class PublicCorpusGraphBuilder:
    """One per backend. v0: GitHub Code Search."""
    backend: Literal["github-code-search"]

    async def build(
        self,
        snapshot: AtlasSnapshot,
        *,
        seed_entity_ids: list[str] | None = None,
        budget: PublicCorpusBudget,
    ) -> CodeGraph: ...
```

Pipeline (GitHub Code Search v0):

1. **Seed.** Either every entity in the Atlas (full mode) or a
   targeted list of seeds (e.g. patterns whose statistical confidence
   is borderline and need corroboration).
2. **Query.** For each seed, issue a code-search query for the
   entity's `qualified_name`. Rate-limit-bucketed at the acquirer.
3. **Filter.**
   - **License allowlist** — only repos with permissively licensed
     content (default: MIT, Apache-2.0, BSD-2/3-Clause, MPL-2.0).
   - **Quality floor** — minimum star count or repo age (configurable).
   - **Deduplication** — collapse forks via GitHub's `is_fork` flag.
4. **Fetch hits.** Pull matched files (cached by SHA).
5. **Parse + resolve.** Use the same language-aware AST extractor as
   `LocalCodeGraph` (Python v0; TS to follow). Resolve symbols against
   the Atlas the same way.
6. **Emit references** with `subject = PUBLIC_CORPUS` and full
   `Provenance` (repo + commit + license + star count).

**Constraints baked in:**

| Constraint | v0 default | Why |
|---|---|---|
| Rate limit | 1 query/second per backend | GitHub's authenticated limit is 30 req/min; default is conservative |
| Per-snapshot budget | 1000 queries | bounds cost and latency |
| Per-entity budget | 5 hits | enough for triangulation, not enough to drown the slice |
| TTL | 7 days | refresh per snapshot or operator-triggered |
| License allowlist | configurable, defaults to permissive | hygiene |
| Star floor | 10 stars | filters trivial repos |
| Per-source opt-in | required | safety default |

**Provenance.** Every public-corpus reference cites repo + commit
SHA + path + license. The user can audit any pattern citation back to
its source.

## Multi-subject merge — `MergedSlice`

When pattern-mining or the discriminator needs context for a target
entity, it asks the `CodeGraphMux` for a `MergedSlice`. The mux
queries each enabled subject's `slice_for` and interleaves the
components.

```python
class MergedSlice(BaseModel):
    target_entity_id: str
    snapshot_id: str
    components: list[SliceComponent]   # tagged with subject
    by_subject_token_count: dict[CodeGraphSubject, int]
    truncated: bool
```

Merge policy:

1. Always include the **definition** from `IngestedCodeGraph` first.
2. Round-robin across enabled subjects within each priority tier
   (callers, callees, examples, etc.) to ensure balanced
   representation.
3. Token budget is split across subjects. Default split: 50%
   ingested, 30% local-project, 20% public-corpus. Rebalances if a
   subject has insufficient content.
4. Public-corpus components carry their `Provenance` inline so the
   LLM (and the discriminator) can see the citation alongside the
   content.

The discriminator's evidence-sufficiency check uses the
per-subject token count to enforce the higher bar for public-corpus
evidence (decision N-12).

## Storage

```sql
CREATE TABLE code_graph_references (
    snapshot_id     TEXT REFERENCES atlas_snapshots(id),
    subject         TEXT NOT NULL,           -- 'ingested' | 'local-project' | 'public-corpus'
    source_entity   TEXT,                    -- nullable
    target_entity   TEXT NOT NULL,
    kind            TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    line_start      INTEGER,
    line_end        INTEGER,
    context         TEXT,
    confidence      REAL NOT NULL,
    provenance_json TEXT,                    -- repo, commit, license, star_count for public-corpus
    PRIMARY KEY (snapshot_id, subject, source_entity, target_entity, kind, file_path, line_start)
);

CREATE INDEX idx_code_graph_target ON code_graph_references(target_entity, snapshot_id, subject);
CREATE INDEX idx_code_graph_source ON code_graph_references(source_entity, snapshot_id) WHERE source_entity IS NOT NULL;
```

The index on `(target_entity, snapshot_id, subject)` is the critical
query path: "every reference *to* this entity, partitioned by which
corpus we found it in."

### Per-subject snapshot semantics

`code_graph_references` is keyed on `snapshot_id`, but the snapshot
identity differs by subject:

- **Ingested** — references inherit the Atlas `snapshot_id` directly.
- **Local-project** — references inherit the Atlas `snapshot_id` of
  the source they reference, but rebuild on local-project content
  changes (debounced).
- **Public-corpus** — references inherit the Atlas `snapshot_id` of
  the source they reference, with a TTL-based refresh policy. A
  public-corpus reference older than its TTL is candidate for refresh
  on next miner run.

## Slicing API — the load-bearing primitive

```python
class CodeGraph(Protocol):
    subject: CodeGraphSubject
    snapshot_id: str

    async def references_to(self, entity_id: str) -> list[Reference]: ...
    async def references_from(self, entity_id: str) -> list[Reference]: ...
    async def callers(self, entity_id: str) -> list[Reference]: ...
    async def callees(self, entity_id: str) -> list[Reference]: ...
    async def examples_using(self, entity_id: str) -> list[ExampleSlice]: ...
    async def co_occurring_entities(self, entity_id: str, *, min_confidence: float = 0.5) -> list[tuple[str, int]]: ...

    async def slice_for(
        self,
        entity_id: str,
        *,
        budget_tokens: int = 8000,
        include: SliceComponents = ...,
    ) -> Slice: ...
```

`slice_for` is the entry point pattern-mining uses. It produces a
`Slice`:

```python
class Slice(BaseModel):
    target_entity_id: str
    snapshot_id: str
    components: list[SliceComponent]   # ordered by relevance
    token_count: int                   # actual count after assembly
    truncated: bool                    # if budget forced trimming

class SliceComponent(BaseModel):
    kind: Literal[
        "definition",        # the entity itself (signature, doc)
        "neighbor",          # related entity in same partition group
        "supertype",         # parent class / interface
        "subtype",           # known subclasses
        "caller",            # other entity that calls this
        "callee",            # entity called by this
        "example",           # example block exercising this entity
        "constraint",        # docstring constraint or prose note about ordering / preconditions
        "co-occurrence",     # entities appearing together with this one
    ]
    content: str
    cite: SourceLocation | None
    score: float             # ranking signal
```

### Slice budgeting

A token budget is mandatory. Without it, an entity in a deeply
connected hub (e.g. `Node` in Godot) produces a slice that exceeds any
LLM context.

Budgeting algorithm:

1. Always include the **definition** (entity signature + doc).
2. Include up to N highest-confidence direct callers / callees.
3. Include up to M examples, preferring those where the target appears
   in the central scope (not just imported).
4. Include direct supertype + immediate-subtype names (no bodies).
5. Include co-occurring entities as a list (names + one-line summaries),
   not full bodies.
6. Include declared constraints from docstring (parsed by the parser's
   constraint extractor when available).

Each component has a static priority. The assembler greedily packs
within budget by priority and within priority by score.

### Slicing strategies per pattern type

Different miners need different slices:

| Pattern kind | Slice emphasis |
|---|---|
| Co-occurrence | Examples + co-occurring-entities list |
| Sequence | Examples (especially ones with multiple statements involving the entity) |
| Idiom | Examples + constraints + supertype/subtype |
| Constraint | Docstring + prose docs + constraint annotations |
| Use case | Wider neighborhood: callers, examples, sibling methods |

`SliceComponents` is a tunable hint passed by the miner.

## Subject extension hooks

The `CodeGraph` Protocol accepts further subjects without consumer
changes. Plausible additions post-v0:

- **Sourcegraph public** — alternative public-corpus backend with
  better ranking; `PublicCorpusGraphBuilder(backend="sourcegraph")`.
- **Software Heritage** — long-tail license-clean corpus.
- **Internal corpora** — enterprise deployments may want their own
  monorepo as a fourth subject (`InternalCorpusGraph`); the same
  Protocol accommodates.
- **Additional local languages** — `LocalCodeGraphBuilder`
  implementations for TypeScript, Go, Rust, etc. v0 ships Python;
  TypeScript is the next target.

Each is a self-contained subject implementation; the merge layer and
consumers don't change.

## What the code graph does NOT do

- **It does not embed.** Slices are passed to the knowledge index for
  embedding when needed, but the graph itself stores raw text + edges.
- **It does not run LLMs.** It is a deterministic preprocessing layer.
- **It does not synthesize patterns.** That is the next module.
- **It does not own the Atlas.** It writes a sibling table; entities
  remain in the Atlas.

## Open implementation choices

- **Symbol resolver fidelity.** `Parser.extract_symbols` is the bridge
  from raw text to Atlas entity ids. Naive extractors miss aliases,
  imports under non-canonical names, etc. v0 ships per-parser
  extractors with reasonable defaults; the long tail is iteration.
- **Constraint extraction from prose.** Parsing "must be called from
  the main thread" out of free-text docs requires either pattern
  matching (cheap, brittle) or LLM extraction (expensive, gated). v0
  ships pattern matching for known phrasings; LLM extraction is
  Phase-5 with discriminator gating.
- **Cross-snapshot reference stability.** Renames across snapshots
  break reference resolution. The Atlas already lacks a rename
  detector; the code graph inherits the limitation.
- **Local-project ↔ ingested-source binding.** When a deployment
  configures multiple ingested sources, a single local-project
  reference might match entities in more than one source. v0
  emits the reference against the first match by source priority
  (a configurable list). Future work: emit against all matches with
  a multi-source disambiguation pass.
- **Public-corpus ranking.** v0 ranks hits by repo star count; this
  is rough. Better signals (recency, contributor diversity, test
  presence) are post-v0.
- **Public-corpus token spend.** Even with rate limits, public-corpus
  mining is a meaningful cost driver if every entity is queried. v0
  ships a "seed-driven" mode where the operator targets entities
  that need corroboration, plus a "full sweep" mode that runs on a
  longer cadence.
