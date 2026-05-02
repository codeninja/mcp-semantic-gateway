# 07 — Module Layout

This document specifies where new code lives in `src/mcp_semantic_gateway/`.
It also documents how existing modules change to accommodate the new
pipeline.

## Existing layout (recap)

```
src/mcp_semantic_gateway/
├── cli/
│   └── main.py
├── config/
│   ├── loader.py
│   └── models.py
├── ingestion/
│   ├── collector.py
│   ├── embedder.py
│   ├── forge.py
│   └── index_writer.py
├── integration/
│   ├── proxy.py
│   └── server.py
├── retrieval/
│   ├── core.py
│   └── query_engine.py
└── storage/
    ├── init.py
    ├── metadata_db.py
    └── vector_store.py
```

## Target layout

```
src/mcp_semantic_gateway/
├── cli/
│   └── main.py                          # adds: ingest, plan, feedback, rollback subcommands
│
├── config/
│   ├── loader.py
│   └── models.py                        # extends: SourceConfig variants, planner config, retrieval config
│
├── atlas/                               # NEW — Atlas representation and storage
│   ├── __init__.py
│   ├── models.py                        # Entity, Edge, AtlasSnapshot, TypeRef, Param
│   ├── hashing.py                       # entity_hash, group_hash, snapshot_hash
│   ├── repository.py                    # SQLite I/O for atlas tables
│   └── classifier.py                    # ChangeClass dispatch (delegates to adapters)
│
├── adapters/                            # NEW — Source Adapters
│   ├── __init__.py
│   ├── base.py                          # SourceAdapter Protocol, RawSnapshot, PartitionMap
│   ├── registry.py                      # adapter discovery + dispatch by source_kind
│   ├── openapi/
│   │   ├── adapter.py                   # consumes & extends ingestion/forge.py
│   │   └── classifier.py                # OpenAPI-specific ChangeClass rules
│   └── godot_xml/
│       ├── adapter.py                   # parses Godot's doc/classes/*.xml
│       ├── classifier.py
│       └── tool_requirements.py
│
├── ingestion/
│   ├── collector.py                     # extended: now drives adapters via adapters/registry
│   ├── embedder.py                      # unchanged for v1; pluggable embedding backends in v2
│   ├── forge.py                         # narrowed: OpenAPI-specific helpers used by adapters/openapi
│   ├── index_writer.py                  # extended: writes Atlas + skills to Qdrant + SQLite
│   ├── use_cases.py                     # NEW — use case generation pipeline
│   └── update_pipeline.py               # NEW — orchestrates surgical updates per [06]
│
├── synthesis/                           # NEW — skill synthesis and discrimination
│   ├── __init__.py
│   ├── prompts.py                       # prompt templates for use-case gen, skill synth, HyDE rewrite
│   ├── skill_synthesizer.py             # produces skill packages
│   ├── description_optimizer.py         # HyDE rewrite of frontmatter description
│   ├── section_assembler.py             # builds .meta.json/sections from generation traces
│   ├── llm_client.py                    # Anthropic SDK wrapper; configurable model
│   └── feedback_aggregator.py           # rolls up skill_feedback rows into scores + triggers
│
├── validation/                          # NEW — discriminator
│   ├── __init__.py
│   ├── discriminator.py                 # orchestrates all passes
│   ├── spec_conformance.py              # Pass 1
│   ├── atlas_grounding.py               # Pass 2 (delegates symbol parsing to adapters)
│   ├── coherence.py                     # Pass 3
│   └── retrieval_fitness.py             # Pass 4
│
├── planning/                            # NEW — task decomposition and binding
│   ├── __init__.py
│   ├── planner.py                       # mcp_semantic_gateway_plan handler
│   ├── decomposer.py                    # LLM-driven sub-goal decomposition
│   ├── binder.py                        # sub-goal → skill | use-case-gap | entity-fallback
│   └── plan_cache.py                    # plan cache keyed by task + filter context
│
├── retrieval/
│   ├── core.py                          # extended: adds RetrievalService protocol
│   ├── query_engine.py
│   ├── service.py                       # NEW — RetrievalService implementation (Qdrant + SQLite)
│   ├── hyde.py                          # NEW — HyDE rewrite (cached)
│   ├── reranker.py                      # NEW — cross-encoder reranking
│   └── graph_expander.py                # NEW — graph-aware expansion over Atlas edges
│
├── storage/
│   ├── init.py                          # extended: project-local .mcp_semantic_gateway/
│   ├── metadata_db.py                   # extended: new tables per [03]
│   ├── vector_store.py                  # REPLACED — now wraps Qdrant client (was hnswlib)
│   ├── qdrant_collections.py            # NEW — collection definitions, payload index migrations
│   └── package_store.py                 # NEW — read/write skill packages on disk
│
└── integration/
    ├── proxy.py                         # extended: adds plan/get_skill/feedback/rollback synthetic tools
    └── server.py                        # extended: HTTP endpoints mirror new MCP tools
```

## Module responsibilities

### `atlas/`

Source-of-truth representation of any ingested API. `models.py` is the
schema; `repository.py` is the SQLite layer; `hashing.py` computes the
content hashes that drive caching; `classifier.py` provides the dispatch
glue but delegates per-source classification rules to adapters.

Nothing in `atlas/` knows about LLMs, retrieval, or skills.

### `adapters/`

One subpackage per source kind. Each implements:
- `acquire()` — fetch raw source bytes
- `parse()` — produce an `AtlasSnapshot`
- `partition_hint()` — group entity ids
- `detect_version()` — extract semantic version
- `classify_entity_change()` — declare what's breaking
- `tool_requirements` — static map of EntityKind → tool primitives
- `extract_symbols(text)` — used by the discriminator's grounding pass

`registry.py` discovers adapters by entry point or by explicit registration.

### `ingestion/`

Orchestration only. The existing `collector.py` is extended to drive
adapters; `forge.py` shrinks to OpenAPI-specific helpers used by the
OpenAPI adapter. Two new modules:

- `use_cases.py` — runs cluster-aware use-case generation per snapshot.
- `update_pipeline.py` — implements the fast-forward / surgical /
  quarantine / regenerate decision tree from [06](06-caching-and-updates.md).

### `synthesis/`

LLM-driven generation. `skill_synthesizer.py` is the entry point; it
calls `description_optimizer.py` and `section_assembler.py` and emits a
skill package on disk via `storage/package_store.py`. `llm_client.py`
wraps the Anthropic SDK and provides configurable model selection,
prompt caching, and retry. `feedback_aggregator.py` is the offline rollup
job.

### `validation/`

Pure validators. `discriminator.py` runs all passes in parallel and
returns a `DiscriminationReport`. Each pass is independently testable.
Atlas grounding delegates symbol extraction to adapters because symbol
syntax is source-specific.

### `planning/`

`planner.py` is the synthetic-tool handler invoked from the proxy.
`decomposer.py` calls the LLM to produce a sub-goal tree. `binder.py`
maps sub-goals onto skills via `RetrievalService`. `plan_cache.py` keys
plans by `hash(task + filter_context)`.

### `retrieval/`

`service.py` is the new central retrieval interface; `core.py` continues
to support the existing tool/prompt retrieval surface. `hyde.py`,
`reranker.py`, and `graph_expander.py` are composable components used by
the service.

### `storage/`

`vector_store.py` is replaced — it wraps the Qdrant client (embedded or
server) behind the same Protocol-shaped interface so callers don't break.
`qdrant_collections.py` declares the three collections (`skills`,
`use_cases`, `entities`) and their payload indexes.
`package_store.py` reads/writes skill package directories on disk and
keeps them in sync with the `skills` table.

### `integration/`

`proxy.py` registers four new synthetic tools alongside the existing
three. `server.py` mirrors them as HTTP endpoints.

### `cli/`

New subcommands:

```
mcp-semantic-gateway ingest <source-id>      # acquire + parse + use-cases + synthesis
mcp-semantic-gateway plan "<task>"           # invoke planner from CLI for debugging
mcp-semantic-gateway feedback <skill> <signal>
mcp-semantic-gateway rollback <skill> <version>
mcp-semantic-gateway atlas list              # inspect Atlas snapshots and entities
mcp-semantic-gateway skills list             # inspect generated skills
```

## Configuration extensions

`config/models.py` gains:

```python
class OpenAPISourceConfig(BaseModel):
    type: Literal["openapi"]
    url: str
    auth: AuthConfig | None = None

class GodotXMLSourceConfig(BaseModel):
    type: Literal["godot-xml"]
    repo: str                              # git url or local path
    ref: str = "stable"                    # tag, branch, or commit
    classes_dir: str = "doc/classes"

class SourceConfig(RootModel):
    root: OpenAPISourceConfig | GodotXMLSourceConfig | MCPServerConfig | SkillSourceConfig

class PlannerConfig(BaseModel):
    decomposer_model: str = "claude-sonnet-4-6"
    max_steps: int = 20
    bind_score_threshold: float = 0.75
    bind_margin_threshold: float = 0.10
    cache_ttl_seconds: int = 300
    synthesis_on_gap: bool = False         # Phase-6

class RetrievalConfig(BaseModel):
    qdrant_url: str = "embedded"           # "embedded" or http(s):// URL
    embedding_model: str = "all-MiniLM-L6-v2"
    reranker_model: str | None = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    hyde_enabled: bool = True
    top_k_per_collection: int = 50
    final_top_k: int = 10
```

## Compatibility with existing code

- `ingestion/collector.py:156` (the existing `SKILL.md` filesystem scan)
  remains. Generated skills live in `.mcp_semantic_gateway/skills/...`
  which the collector also scans, so generated and hand-authored skills
  index identically.
- `integration/proxy.py:24` (the proxy entry point) and the existing
  three synthetic tools are unchanged. New tools are additive.
- `tests/test_e2e.py` and friends continue to pass with the existing
  surface mocked. New tests cover new modules.

## Test layout

```
tests/
├── test_e2e.py                          # existing
├── test_openapi_ingestion.py            # existing
├── test_skills_ingestion.py             # existing
├── adapters/
│   ├── test_openapi_adapter.py
│   └── test_godot_xml_adapter.py
├── atlas/
│   ├── test_models.py
│   ├── test_hashing.py
│   └── test_repository.py
├── ingestion/
│   ├── test_use_cases.py
│   └── test_update_pipeline.py
├── synthesis/
│   ├── test_skill_synthesizer.py        # uses fixture LLM responses
│   └── test_description_optimizer.py
├── validation/
│   ├── test_spec_conformance.py
│   ├── test_atlas_grounding.py
│   ├── test_coherence.py
│   └── test_retrieval_fitness.py
├── planning/
│   ├── test_planner.py
│   ├── test_decomposer.py
│   └── test_binder.py
├── retrieval/
│   ├── test_service.py                  # uses Qdrant embedded
│   ├── test_hyde.py
│   └── test_graph_expander.py
└── integration/
    └── test_proxy_planner.py
```

LLM-driven modules use cassette-style fixtures (recorded model
responses) for reproducibility. Live LLM tests gate on an env var.

## What this layout deliberately avoids

- A monolithic `synthesis_pipeline.py` that knows about Atlas, planning,
  and storage. Each subpackage has one job.
- Inheritance hierarchies for adapters / discriminators. Protocols + flat
  module structures keep the dispatch obvious.
- Premature abstraction over LLM providers. `llm_client.py` is
  Anthropic-specific in v1; if a second provider arrives, refactor then.
