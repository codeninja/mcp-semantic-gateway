# 11 — Module Layout

This document specifies where new code lives in
`src/mcp_semantic_gateway/`. The layout reflects the modular
architecture from [01-modules-and-contracts.md](01-modules-and-contracts.md):
each module is a Python subpackage; the dependency direction matches
the tier ordering.

## Existing layout (recap)

```
src/mcp_semantic_gateway/
├── cli/main.py
├── config/loader.py, models.py
├── ingestion/collector.py, embedder.py, forge.py, index_writer.py
├── integration/proxy.py, server.py
├── retrieval/core.py, query_engine.py
└── storage/init.py, metadata_db.py, vector_store.py
```

## Target layout

```
src/mcp_semantic_gateway/
├── cli/
│   └── main.py                          # adds: ingest, query, plan, feedback subcommands
│
├── config/
│   ├── loader.py
│   └── models.py                        # SourceConfig, IndexConfig, PlannerConfig, SkillGenConfig
│
├── storage/                             # Tier 0
│   ├── init.py                          # project-local .mcp_semantic_gateway/
│   ├── metadata_db.py                   # extended with new tables
│   ├── vector_store.py                  # REPLACED — Qdrant client wrapper
│   ├── qdrant_collections.py            # collection definitions, payload indexes
│   └── package_store.py                 # skill package on-disk I/O (used by skill-gen)
│
├── sources/                             # Tier 1 — knowledge gathering
│   ├── __init__.py
│   ├── refs.py                          # SourceRef polymorphism
│   ├── raw.py                           # RawSnapshot
│   ├── acquirers/
│   │   ├── base.py                      # Acquirer Protocol
│   │   ├── local_path.py
│   │   ├── http.py
│   │   ├── git.py
│   │   ├── local_project.py             # for LocalCodeGraph
│   │   ├── github_code_search.py        # for PublicCorpusGraph (v0 backend)
│   │   └── registry.py
│   ├── parsers/
│   │   ├── base.py                      # Parser Protocol
│   │   ├── openapi.py                   # absorbs ingestion/forge.py
│   │   ├── godot_xml.py
│   │   ├── sphinx.py                    # later
│   │   ├── dts.py                       # later
│   │   ├── markdown.py
│   │   └── registry.py
│   └── pipeline.py                      # acquire → parse → AtlasSnapshot
│
├── atlas/                               # Tier 1
│   ├── __init__.py
│   ├── models.py                        # Entity, Edge, AtlasSnapshot, Pattern (record), TypeRef, Param
│   ├── hashing.py                       # entity_hash, semantic_hash, group_hash, snapshot_hash, pattern_hash
│   ├── repository.py                    # AtlasRepository — SQLite I/O
│   └── classifier.py                    # ChangeClass dispatch (delegates to parsers)
│
├── code_graph/                          # Tier 2 — semantic understanding
│   ├── __init__.py
│   ├── models.py                        # Reference, ReferenceKind, Provenance, Slice, MergedSlice, SliceComponent
│   ├── builder.py                       # CodeGraphBuilder Protocol
│   ├── builders/                        # one per parser content_kind (ingested subject)
│   │   ├── openapi.py
│   │   ├── godot_xml.py
│   │   ├── sphinx.py                    # later
│   │   └── dts.py                       # later
│   ├── language_ast/                    # AST extractors used by local-project + public-corpus subjects
│   │   ├── base.py                      # LanguageASTExtractor Protocol
│   │   ├── python.py                    # v0 minimum
│   │   └── typescript.py                # phase 3.5 / next
│   ├── public_backends/                 # PublicCorpusGraph backends
│   │   ├── base.py                      # PublicCorpusBackend Protocol
│   │   └── github_code_search.py        # v0 backend
│   ├── repository.py                    # SQLite I/O for code_graph_references (multi-subject)
│   ├── slicer.py                        # slice_for(entity_id) → Slice
│   ├── mux.py                           # CodeGraphMux: merge slices across enabled subjects
│   └── subjects/
│       ├── ingested.py                  # IngestedCodeGraph (always on)
│       ├── local_project.py             # LocalCodeGraph (v0 with Python AST)
│       └── public_corpus.py             # PublicCorpusGraph (v0 with GitHub Code Search)
│
├── pattern_mining/                      # Tier 2
│   ├── __init__.py
│   ├── models.py                        # Pattern, PatternBody, Participant, Evidence
│   ├── store.py                         # PatternStore — SQLite I/O
│   ├── miners/
│   │   ├── base.py                      # PatternMiner Protocol
│   │   ├── co_occurrence.py             # Tier 1 (deterministic)
│   │   ├── sequence.py                  # Tier 1
│   │   ├── idiom_cluster.py             # Tier 2 (statistical)
│   │   ├── constraint.py                # Tier 2
│   │   ├── llm_pattern.py               # Tier 3 (LLM-induced)
│   │   ├── use_case.py                  # Tier 3
│   │   └── registry.py
│   ├── prompts.py                       # prompt templates for LLM miners
│   ├── llm_client.py                    # Anthropic SDK wrapper, prompt caching
│   └── pipeline.py                      # orchestrate Tier 1 → 2 → 3 with budgets
│
├── knowledge_index/                     # Tier 3 — queryable surface
│   ├── __init__.py
│   ├── service.py                       # KnowledgeIndex Protocol implementation
│   ├── hyde.py                          # HyDE rewriter (cached)
│   ├── reranker.py                      # cross-encoder reranking
│   ├── graph_expander.py                # graph-aware expansion
│   ├── filters.py                       # Filters builder
│   └── embeddings.py                    # embedding model wrapper
│
├── knowledge_mcp/                       # Tier 3
│   ├── __init__.py
│   ├── tools.py                         # query_api, describe_entity, find_patterns, expand, local_usage
│   └── handlers.py                      # MCP tool handlers; registered by integration/proxy
│
├── updates/                             # Tier 4 — lifecycle
│   ├── __init__.py
│   ├── pipeline.py                      # surgical / quarantine / regenerate decision tree
│   ├── classifier.py                    # ChangeClass coordination
│   └── reports.py                       # SnapshotIngestReport
│
├── task_decomp/                         # Tier 5 — consumer
│   ├── __init__.py
│   ├── planner.py                       # Planner Protocol implementation
│   ├── decomposer.py                    # LLM-driven sub-goal decomposition
│   ├── binder.py                        # sub-goal → KnowledgeBinding | SkillBinding | Gap
│   ├── plan_cache.py
│   └── mcp_tools.py                     # mcp_semantic_gateway_plan handler
│
├── skill_gen/                           # Tier 5 — consumer
│   ├── __init__.py
│   ├── synthesizer.py                   # SkillSynthesizer Protocol implementation
│   ├── description_optimizer.py         # HyDE rewrite of frontmatter description
│   ├── section_assembler.py             # builds .meta.json/sections
│   ├── package_builder.py               # writes SKILL.md + supporting files
│   ├── retrieval.py                     # SkillRetrieval Protocol implementation
│   ├── feedback_aggregator.py
│   ├── prompts.py
│   └── mcp_tools.py                     # get_skill, submit_feedback, rollback
│
├── validation/                          # Tier 5 — used by pattern_mining and skill_gen
│   ├── __init__.py
│   ├── discriminator.py                 # orchestrates passes in parallel
│   ├── spec_conformance.py
│   ├── atlas_grounding.py               # delegates symbol parsing to parsers
│   ├── pattern_attribution.py           # NEW
│   ├── coherence.py
│   └── retrieval_fitness.py
│
└── integration/
    ├── proxy.py                         # registers tools conditionally on enabled modules
    └── server.py                        # HTTP mirrors for all MCP tools
```

## Module-to-package mapping

| Logical module | Python package | Tier |
|---|---|---|
| storage | `storage/` | 0 |
| config | `config/` | 0 |
| sources | `sources/` | 1 |
| atlas | `atlas/` | 1 |
| code-graph | `code_graph/` | 2 |
| pattern-mining | `pattern_mining/` | 2 |
| knowledge-index | `knowledge_index/` | 3 |
| knowledge-mcp | `knowledge_mcp/` | 3 |
| updates | `updates/` | 4 |
| task-decomp | `task_decomp/` | 5 |
| skill-gen | `skill_gen/` | 5 |
| validation | `validation/` | 5 |

Underscored package names mirror the logical hyphenated names from
[01](01-modules-and-contracts.md).

## Compatibility with existing code

- `ingestion/forge.py` is decomposed: parsing logic moves to
  `sources/parsers/openapi.py`; orchestration logic is replaced by
  `sources/pipeline.py`.
- `ingestion/collector.py` survives but narrows to scanning
  hand-authored skills + tools. Generated skills surface through
  `skill_gen/package_builder.py` write paths.
- `ingestion/embedder.py` survives as embedding utility; consumed by
  `knowledge_index/embeddings.py`.
- `ingestion/index_writer.py` is split: Atlas writes go to
  `atlas/repository.py`; Qdrant writes go to module-specific writers.
- `retrieval/core.py` and `retrieval/query_engine.py` survive for the
  existing tool/prompt surface; the new modules don't import them.
- `storage/vector_store.py` is rewritten to wrap Qdrant.
- `integration/proxy.py:24` survives unchanged. New tools added at
  registration time.

The `ingestion/` directory is deliberately retained for the
hand-authored-content path (existing tools/prompts/skills). It does
not gain new responsibilities in this design.

## Configuration extensions

```python
# config/models.py

class HTTPSourceConfig(BaseModel):
    type: Literal["http"]
    url: str
    auth: AuthConfig | None = None

class GitSourceConfig(BaseModel):
    type: Literal["git"]
    repo: str
    ref: str = "HEAD"
    subpath: str | None = None

class LocalPathSourceConfig(BaseModel):
    type: Literal["local-path"]
    path: str

class SourceConfig(BaseModel):
    id: str
    ref: HTTPSourceConfig | GitSourceConfig | LocalPathSourceConfig
    parser: str                            # "openapi" | "godot-xml" | ...
    auth: AuthConfig | None = None

class IndexConfig(BaseModel):
    qdrant_url: str = "embedded"
    embedding_model: str = "bge-small-en-v1.5"
    reranker_model: str | None = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    hyde_enabled: bool = True

class LocalCodeGraphConfig(BaseModel):
    enabled: bool = False                       # opt-in
    project_root: str = "."
    languages: list[str] = ["python"]           # v0
    file_globs_include: list[str] = ["**/*.py"]
    file_globs_exclude: list[str] = [".venv/**", "**/site-packages/**"]
    local_safe_llms_only: bool = True

class PublicCorpusGraphConfig(BaseModel):
    enabled: bool = False                       # opt-in per source
    backend: Literal["github-code-search"] = "github-code-search"
    github_token_env: str = "GITHUB_TOKEN"
    license_allowlist: list[str] = ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "MPL-2.0"]
    rate_limit_qps: float = 1.0
    queries_per_snapshot: int = 1000
    hits_per_entity: int = 5
    star_floor: int = 10
    ttl_days: int = 7

class CodeGraphConfig(BaseModel):
    ingested: bool = True
    local_project: LocalCodeGraphConfig = LocalCodeGraphConfig()
    public_corpus: PublicCorpusGraphConfig = PublicCorpusGraphConfig()

class PatternMiningConfig(BaseModel):
    deterministic: bool = True
    statistical: bool = True
    llm_induced: bool = False
    llm_model: str = "claude-sonnet-4-6"
    budget_tokens_per_snapshot: int = 1_000_000
    public_corpus_corroboration: bool = True   # turns on the targeted CorroborationMiner

class PlannerConfig(BaseModel):
    enabled: bool = True
    decomposer_model: str = "claude-sonnet-4-6"
    bind_score_threshold: float = 0.75

class SkillGenConfig(BaseModel):
    enabled: bool = False                  # opt-in
    synthesis_model: str = "claude-sonnet-4-6"
    discriminator_strict: bool = True
```

## Test layout

```
tests/
├── test_e2e.py                          # existing
├── test_openapi_ingestion.py            # existing
├── test_skills_ingestion.py             # existing
├── sources/
│   ├── test_acquirers_local.py
│   ├── test_acquirers_http.py
│   ├── test_acquirers_git.py            # uses git fixture
│   ├── parsers/
│   │   ├── test_openapi.py
│   │   ├── test_godot_xml.py
│   │   └── test_markdown.py
│   └── test_composition.py              # contract test: every (acquirer, parser) combo round-trips
├── atlas/
│   ├── test_models.py
│   ├── test_hashing.py
│   └── test_repository.py
├── code_graph/
│   ├── test_builder_godot.py
│   ├── test_slicer.py
│   ├── test_references.py
│   ├── test_mux.py                      # multi-subject merge
│   ├── language_ast/
│   │   └── test_python.py
│   ├── subjects/
│   │   ├── test_local_project.py        # uses fixture project tree
│   │   └── test_public_corpus.py        # uses cassette-recorded GitHub responses
│   └── public_backends/
│       └── test_github_code_search.py
├── pattern_mining/
│   ├── test_co_occurrence.py
│   ├── test_sequence.py
│   ├── test_idiom_cluster.py
│   └── test_llm_pattern.py              # cassette fixtures
├── knowledge_index/
│   ├── test_service.py                  # uses Qdrant embedded
│   ├── test_hyde.py
│   └── test_graph_expander.py
├── knowledge_mcp/
│   └── test_tools.py
├── updates/
│   └── test_pipeline.py                 # synthetic diffs
├── task_decomp/
│   ├── test_planner.py
│   ├── test_decomposer.py
│   └── test_binder.py
├── skill_gen/
│   ├── test_synthesizer.py              # cassette fixtures
│   └── test_discriminator.py
└── integration/
    └── test_proxy_knowledge.py
```

LLM-driven modules use cassette-style fixtures (recorded model
responses) for reproducibility. Live LLM tests gate on an env var.

## What this layout deliberately avoids

- **A single ingestion subpackage** that conflates fetch + parse +
  index + synthesis. Each tier has its own subpackage.
- **Inheritance hierarchies** for parsers / acquirers / miners.
  Protocol + flat module structures keep dispatch obvious.
- **Premature abstraction over LLM providers.** `llm_client.py` is
  Anthropic-specific in v1; refactor on demand.
- **Duplicating the existing `retrieval/` package.** It survives for
  the legacy hand-authored surface; new modules don't extend it.
