# 01 — Modules and Contracts

This document is the integration spine. It names every module, declares
its public Protocol, and specifies what it is and is not allowed to know
about.

The design discipline is strict: **a module's contract is what other
modules import; everything else is implementation.** Tests target the
contract. Refactors below the contract should never break callers.

## Module roster

```
Tier 0 — Foundations (no internal deps)
  storage         project-local layout, SQLite migrations, Qdrant client wrap
  config          source configs, retrieval config, planner config, skill config

Tier 1 — Knowledge gathering
  sources         SourceRef, Acquirer, Parser, registry, RawSnapshot
  atlas           Entity, Edge, AtlasSnapshot, hashing, repository

Tier 2 — Semantic understanding
  code-graph      AST extraction, ref graph, slicing API, CodeGraph subject
  pattern-mining  pattern types, miners, LLM synthesis, pattern store

Tier 3 — Queryable surface
  knowledge-index Qdrant collections, hybrid retrieval, HyDE, rerank, graph expand
  knowledge-mcp   synthetic MCP tools: query_api, describe_entity, find_patterns, expand

Tier 4 — Lifecycle
  updates         change classification, surgical updates, quarantine, snapshot lifecycle

Tier 5 — Consumers (parallel; either or both can be omitted)
  task-decomp     planner; consumes knowledge-mcp
  skill-gen       skill packages; consumes knowledge-mcp + task-decomp
  validation      discriminator; called by skill-gen and pattern-mining
```

A module may depend only on modules in a strictly lower tier (or another
module in the same tier *if* it's listed adjacent on the same line). No
upward references — `atlas` does not know about `pattern-mining`,
`pattern-mining` does not know about `knowledge-mcp`, etc.

## Contracts

Each module exports a small Protocol or set of Protocols. These are the
only types other modules import. Concrete classes live behind them.

### `sources`

```python
class SourceRef(BaseModel):
    """Where a source lives. Polymorphic by kind."""
    kind: Literal["local-path", "http", "git"]
    # local-path:  path: str
    # http:        url: str, etag: str | None
    # git:         repo: str, ref: str, subpath: str | None

class RawSnapshot(BaseModel):
    """Bytes acquired from a source, content-addressed."""
    source_ref: SourceRef
    raw_hash: str          # sha256 of canonicalized bytes
    artifacts: dict[str, bytes]  # logical-name -> bytes (some sources are multi-file)
    acquired_at: datetime

class Acquirer(Protocol):
    """How to obtain raw bytes. Format-agnostic."""
    supported_kinds: set[str]            # which SourceRef.kind values

    async def acquire(self, ref: SourceRef) -> RawSnapshot: ...
    async def detect_changes(self, prior: RawSnapshot) -> ChangeSignal: ...

class Parser(Protocol):
    """How to interpret bytes for a content kind. Acquisition-agnostic."""
    content_kind: str                    # 'openapi' | 'godot-xml' | 'sphinx' | '.d.ts' | ...

    async def parse(self, raw: RawSnapshot) -> AtlasSnapshot: ...
    def partition_hint(self, snapshot: AtlasSnapshot) -> PartitionMap: ...
    def extract_symbols(self, text: str) -> list[SymbolRef]: ...
    def detect_version(self, raw: RawSnapshot) -> SourceVersion: ...
    def classify_entity_change(self, prior: Entity | None, current: Entity | None) -> ChangeClass: ...
    tool_requirements: dict[EntityKind, list[str]]

class Source(BaseModel):
    """Composition. Resolved at config-load time."""
    id: str                              # 'godot', 'stripe', etc.
    acquirer: Acquirer
    parser: Parser
    config: SourceConfig
```

`Source` is *not* an adapter Protocol. Adapters are gone in this design;
the composition `(Acquirer, Parser)` replaces them. See
[02-sources.md](02-sources.md) for the full rationale.

### `atlas`

```python
class AtlasRepository(Protocol):
    async def write_snapshot(self, snapshot: AtlasSnapshot) -> None: ...
    async def read_snapshot(self, snapshot_id: str) -> AtlasSnapshot: ...
    async def latest_snapshot(self, source_id: str) -> AtlasSnapshot | None: ...
    async def diff_snapshots(self, prior_id: str, current_id: str) -> SnapshotDiff: ...

    async def read_entity(self, entity_id: str, snapshot_id: str) -> Entity: ...
    async def list_entities(self, snapshot_id: str, *, kind: EntityKind | None = None) -> Iterable[Entity]: ...
    async def neighbors(self, entity_id: str, snapshot_id: str, edge_kinds: set[EdgeKind]) -> list[Edge]: ...
```

### `code-graph`

```python
class CodeGraphSubject(Enum):
    """Which corpus the graph is built over. All three ship in v0."""
    INGESTED = "ingested"            # source's own corpus
    LOCAL_PROJECT = "local-project"  # the user's repo
    PUBLIC_CORPUS = "public-corpus"  # external code search

class CodeGraph(Protocol):
    subject: CodeGraphSubject
    snapshot_id: str                 # links to AtlasSnapshot

    async def references(self, entity_id: str) -> list[Reference]: ...
    async def callers(self, entity_id: str) -> list[Reference]: ...
    async def callees(self, entity_id: str) -> list[Reference]: ...
    async def examples_using(self, entity_id: str) -> list[ExampleSlice]: ...
    async def slice_for(self, entity_id: str, *, budget_tokens: int = 8000) -> Slice: ...

class CodeGraphMux(Protocol):
    """Composes enabled subjects into a single slicing surface."""
    enabled_subjects: list[CodeGraphSubject]

    async def slice_for(
        self,
        entity_id: str,
        *,
        budget_tokens: int = 8000,
        subjects: set[CodeGraphSubject] | None = None,   # None = all enabled
    ) -> MergedSlice: ...
```

A `Slice` is a token-budgeted bundle of definition + neighbors + examples.
A `MergedSlice` interleaves slices from multiple subjects with provenance
tags. See [04-code-graph.md](04-code-graph.md).

### `pattern-mining`

```python
class PatternMiner(Protocol):
    """One miner per strategy. All produce Pattern entities."""
    strategy: Literal["co-occurrence", "sequence", "idiom", "constraint", "use-case"]
    determinism: Literal["deterministic", "statistical", "llm-induced"]

    async def mine(self, snapshot_id: str, *, scope: MiningScope) -> AsyncIterator[Pattern]: ...

class PatternStore(Protocol):
    async def write(self, pattern: Pattern) -> None: ...
    async def read(self, pattern_id: str) -> Pattern: ...
    async def list_for_entity(self, entity_id: str) -> list[Pattern]: ...
    async def list_by_kind(self, snapshot_id: str, kind: PatternKind) -> Iterable[Pattern]: ...
```

### `knowledge-index`

```python
class KnowledgeIndex(Protocol):
    """Hybrid retrieval over entities, patterns, and use cases."""
    async def find_entities(self, query: str, *, filters: Filters, top_k: int = 10) -> list[EntityHit]: ...
    async def find_patterns(self, query: str, *, filters: Filters, top_k: int = 10) -> list[PatternHit]: ...
    async def find_use_cases(self, query: str, *, filters: Filters, top_k: int = 10) -> list[UseCaseHit]: ...
    async def expand(self, entity_ids: list[str], *, depth: int = 1) -> list[EntityHit]: ...
```

The skills collection (when skill-gen is online) is owned by skill-gen,
not knowledge-index. Skill-gen registers a parallel `find_skills` call
through the knowledge surface; knowledge-index does not import it.

### `knowledge-mcp`

Stateless. Composes the above into MCP tool handlers. See
[07-knowledge-mcp-surface.md](07-knowledge-mcp-surface.md).

### `updates`

```python
class UpdatePipeline(Protocol):
    async def reingest(self, source_id: str) -> SnapshotIngestReport: ...
    async def classify(self, prior: AtlasSnapshot, current: AtlasSnapshot) -> SnapshotDiff: ...
    async def apply_surgical(self, diff: SnapshotDiff) -> None: ...
```

Surgical updates here cover Atlas + patterns + use cases. Surgical updates
to skills live in skill-gen.

### `task-decomp` (consumer)

```python
class Planner(Protocol):
    async def plan(self, task: str, options: PlanOptions) -> Plan: ...

class Plan(BaseModel):
    plan_id: str
    decomposition: list[SubGoal]

class SubGoal(BaseModel):
    sub_goal: str
    knowledge_bindings: list[KnowledgeBinding]   # entities/patterns/use-cases the sub-goal binds to
    skill_binding: SkillBinding | None           # populated only if skill-gen is online and a skill matches
    gap: Gap | None
```

The `KnowledgeBinding` shape is the planner's primary output. Skill
binding is opportunistic.

### `skill-gen` (consumer)

```python
class SkillSynthesizer(Protocol):
    async def synthesize(self, use_case_id: str, *, snapshot_id: str) -> SkillPackage: ...
    async def regenerate_section(self, skill_id: str, section_path: str) -> SkillPackage: ...

class SkillRetrieval(Protocol):
    async def find_skills(self, query: str, *, filters: Filters, top_k: int = 10) -> list[SkillHit]: ...
```

`SkillRetrieval` is the protocol the planner imports when skill-gen is
present. It satisfies the same shape as a `find_*` method on
`KnowledgeIndex`, so the planner treats it uniformly.

## Integration boundaries — what each module does NOT know

| Module | Does NOT know about |
|---|---|
| `sources` | Atlas internals; embedding models; LLMs |
| `atlas` | Sources; LLMs; Qdrant; skills |
| `code-graph` | LLMs; Qdrant; skills; the planner |
| `pattern-mining` | Qdrant collection names; skills; the planner |
| `knowledge-index` | Pattern *generation* (only consumes Pattern records); LLMs other than HyDE |
| `knowledge-mcp` | Anything below the knowledge-index — it's a thin handler layer |
| `updates` | Skills (skill updates are owned by skill-gen) |
| `task-decomp` | How patterns/use cases were generated; skill internals |
| `skill-gen` | The planner's decomposition algorithm |

## Dispatch and ownership

- **Source dispatch** — `sources/registry.py` maps `(kind, content_kind)`
  pairs to `(Acquirer, Parser)` instances. Configuration declares
  `source_ref` (kind + URI) and `parser` (content kind); the registry
  composes.
- **Pattern miner dispatch** — `pattern-mining/registry.py` lists miners
  in their declared `determinism` order. The pipeline runs deterministic
  miners first; statistical second; LLM-induced last (and gated on
  validation).
- **Knowledge index ownership** — one `KnowledgeIndex` instance per
  process. Multi-tenant filtering is a payload concern, not a separate
  index.
- **MCP tool registration** — `integration/proxy.py` imports the four
  knowledge-MCP tools always, and the planner/skill tools conditionally
  on the consumer modules being installed.

## Configuration shape

```toml
# .mcp_semantic_gateway/config.toml

[sources.godot]
ref = { kind = "git", repo = "https://github.com/godotengine/godot", ref = "4.4-stable", subpath = "doc/classes" }
parser = "godot-xml"

[sources.stripe]
ref = { kind = "http", url = "https://api.stripe.com/openapi.json" }
parser = "openapi"
auth = { type = "bearer", env = "STRIPE_API_KEY" }

[sources.local-pandas]
ref = { kind = "local-path", path = "./vendor/pandas/docs/_build/html" }
parser = "sphinx"

[knowledge-index]
qdrant_url = "embedded"
embedding_model = "bge-small-en-v1.5"
reranker_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"

[code-graph]
ingested = true                                                # always on
local_project = { enabled = true, root = "." }                 # opt-in per deployment
public_corpus = { enabled = false, backend = "github-code-search",
                  github_token_env = "GITHUB_TOKEN",
                  license_allowlist = ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "MPL-2.0"],
                  rate_limit_qps = 1 }                         # off by default

[pattern-mining]
deterministic = true
statistical = true
llm_induced = false       # opt-in once stable

[task-decomp]              # optional consumer
enabled = true

[skill-gen]                # optional consumer
enabled = false           # off by default until Phase 7+
```

A deployment that wants only the knowledge gateway can omit the
`task-decomp` and `skill-gen` sections; their modules refuse to register
without config.

## Module-level testing strategy

- **Unit** — every Protocol gets a fixture mock; module logic tests run
  against the mock, not against concrete implementations.
- **Contract** — for each Protocol, a contract test runs every concrete
  implementation through the same set of behavioral assertions. New
  parsers/acquirers/miners run the contract suite for free.
- **Integration** — small fixtures end-to-end, fixture-recorded LLM
  responses for any LLM-using path.

The contract-test pattern is the lever that keeps adding new parsers
cheap as the matrix grows.
