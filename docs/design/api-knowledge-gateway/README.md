# API Knowledge Gateway — Modular Design

This directory is a reshape of the proposal originally drafted in
[../api-introspection-skills/](../api-introspection-skills/). The earlier
plan put **skill synthesis** at the center of gravity; everything else was a
means to that output. This plan inverts that. The **API knowledge layer is
the product**: a modular pipeline that gathers third-party API surfaces
(local or remote), constructs a semantic understanding rich enough to mine
implementation patterns, and exposes that understanding through a queryable
RAG surface. Skill generation and task decomposition become *consumers* of
the knowledge layer — separate modules with their own lifecycles.

The earlier directory is preserved unchanged. Decisions still in force are
carried forward and marked as inheritable; new decisions are flagged.

## The three pillars

This design is organized around three responsibilities, each owned by a
distinct module group.

### 1. API knowledge gathering

Acquire third-party API surfaces from heterogeneous sources — OpenAPI
schemas, class-reference XML, Sphinx HTML, `.d.ts` files, repository
checkouts, doc sites — and produce a unified, content-addressed
representation. **Local and remote sources are first-class peers**: the
`Acquirer` and `Parser` Protocols are split so that any combination of
*how-to-fetch* and *how-to-parse* is expressible without writing a new
adapter.

Modules: [02-sources.md](02-sources.md), [03-atlas.md](03-atlas.md).

### 2. Semantic understanding

Beyond entities and edges, *understand how the code is used*. For each
entity, build a code graph (AST + reference graph + example provenance)
and a slicing facility that, given any method or interface, can extract
the surrounding context across the corpus and feed that slice to an LLM
to induce **implementation patterns** and **use cases**. Patterns are
first-class, persisted, hashed, and queryable — not transient artifacts
of skill generation.

Modules: [04-code-graph.md](04-code-graph.md),
[05-pattern-mining.md](05-pattern-mining.md).

### 3. RAG-style queryable surface

Ship a knowledge-query API that agents can use directly without going
through skills or planners. Hybrid retrieval (dense + sparse + payload
filters), HyDE rewriting, cross-encoder reranking, and graph-aware
expansion compose into a small set of synthetic MCP tools that return
entities, patterns, and use cases. This is the **earliest shippable
product**.

Modules: [06-knowledge-index.md](06-knowledge-index.md),
[07-knowledge-mcp-surface.md](07-knowledge-mcp-surface.md).

## Module dependency graph

```
                    ┌─────────────┐
                    │   sources   │  ← local + remote, Acquirer ∘ Parser
                    └──────┬──────┘
                           │ AtlasSnapshot
                           ▼
                    ┌─────────────┐
                    │    atlas    │  ← entities + edges, content-hashed
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
       ┌─────────────┐           ┌─────────────┐
       │ code-graph  │  ─slice─▶ │ pattern-    │
       │ (AST + refs)│           │ mining      │
       └──────┬──────┘           └──────┬──────┘
              │                         │ Patterns, UseCases
              └────────────┬────────────┘
                           ▼
                    ┌─────────────┐
                    │ knowledge-  │  ← Qdrant collections, hybrid retrieval
                    │ index       │
                    └──────┬──────┘
                           ▼
                ┌──────────────────────┐
                │ knowledge-mcp-surface│  ◀── ships here (v1 product line)
                │  query_api,          │
                │  describe_entity,    │
                │  find_patterns,      │
                │  expand              │
                └─────┬─────────┬──────┘
                      │         │
            consumers:│         │
                      ▼         ▼
            ┌──────────────┐ ┌────────────────┐
            │ task-        │ │ skill-         │
            │ decomposition│ │ generation     │
            │ (planner)    │ │ (skills v2)    │
            └──────────────┘ └────────────────┘
```

Two principles enforced by this graph:

1. **Skills are not a hard dependency.** Removing the skill-generation
   module leaves a fully functional knowledge gateway.
2. **The planner does not call skills directly.** It calls the knowledge
   surface; if a skill exists for the bound sub-goal, the surface returns
   it as one possible result alongside entities, patterns, and use cases.

## Glossary

Inheritable from earlier design (unchanged):

- **Source** — a third-party API we ingest.
- **Atlas** — unified, source-agnostic representation. Graph of entities
  + edges.
- **Atlas Snapshot** — frozen, content-hashed Atlas for one source version.
- **Use Case** — synthesized description of a real-world task one or more
  Atlas entities support.
- **Skill Package** — agent-skills-spec-conformant directory; now produced
  by the skill-generation module specifically.

New in this design:

- **Acquirer** — module that obtains raw bytes for a `SourceRef`
  (local-path, HTTP URL, git ref). Format-agnostic.
- **Parser** — module that interprets raw bytes for a content kind
  (OpenAPI, Godot-XML, Sphinx, `.d.ts`). Acquisition-agnostic.
- **Source Composition** — `Source = Acquirer ∘ Parser`. Decouples
  fetching from interpreting.
- **Code Graph** — graph layered over the Atlas: AST nodes for source
  kinds that ship code, doc tree + ref graph for structured doc sources,
  symbol-extracted relationships for free-text sources. Three concrete
  subjects, all shipping in v0 with bounded scope: `IngestedCodeGraph`,
  `LocalCodeGraph`, `PublicCorpusGraph`.
- **Slice** — a relevant context window assembled from the Code Graph for
  a target entity: definition, callers, callees, sibling methods, related
  examples, docs that mention it. Bounded by token budget.
- **Pattern** — first-class entity describing how Atlas entities are used
  together. Subkinds: co-occurrence, sequence, idiom, constraint.
- **Knowledge Surface** — the MCP tools exposed by
  `knowledge-mcp-surface`: `query_api`, `describe_entity`, `find_patterns`,
  `expand`.

## Decisions log

Inheritable decisions (carried forward from
[../api-introspection-skills/](../api-introspection-skills/)):

| # | Decision | Source |
|---|---|---|
| I-1 | Vector store is Qdrant (embedded for dev, server for prod) | prior #10 |
| I-2 | Content-addressed hashing at every level (entity, group, snapshot) | prior |
| I-3 | Project-local data directory `.mcp_semantic_gateway/` | prior #18 |
| I-4 | Atlas is unified across source types; adapters normalize | prior #4 |
| I-5 | Atlas versioning is per-source (one snapshot per version) | prior #5 |
| I-6 | Section granularity for any skill output is auto-derived from partition hints | prior #6 |
| I-7 | Skill format follows the agent-skills spec; `.meta.json` sidecar carries non-spec metadata | prior #1, #3 |
| I-8 | Skill bodies are spec-conformant procedural prose | prior #8 |
| I-9 | `allowed-tools` derived automatically | prior #12 |
| I-10 | Auth passes through from MCP source config; skills/queries reference auth by name | prior #11 |
| I-11 | Cross-major source bumps regenerate cold; minor/patch use surgical updates | prior #14 |
| I-12 | Breaking changes auto-quarantine affected skills; no auto-repair | prior #16 |
| I-13 | Static discriminator for v1; LLM-judge planned later | prior #7 |
| I-14 | Godot is the non-OpenAPI integration target | prior #17 |

New decisions for this reshape:

| # | Decision | Doc |
|---|---|---|
| N-1 | `Acquirer` and `Parser` are separate Protocols; `Source = Acquirer ∘ Parser` | [02](02-sources.md) |
| N-2 | Local and remote sources are first-class peers — every parser must work over both | [02](02-sources.md) |
| N-3 | All three `CodeGraph` subjects (`IngestedCodeGraph`, `LocalCodeGraph`, `PublicCorpusGraph`) ship in v0; pattern mining receives merged slices across enabled subjects | [04](04-code-graph.md) |
| N-4 | Patterns are first-class Atlas entities (`Pattern` kind) with their own hash, embedding, and Qdrant collection | [05](05-pattern-mining.md) |
| N-5 | Pattern mining proceeds in a deterministic→statistical→LLM-induced gradient; LLM-induced patterns require discriminator grounding before publish | [05](05-pattern-mining.md) |
| N-6 | The knowledge-MCP-surface ships before skills. Skills are a Phase-7+ consumer module | [12](12-phasing.md) |
| N-7 | Task decomposition (planner) and skill generation are separate modules; both consume the knowledge surface, neither calls the other | [09](09-task-decomposition.md), [10](10-skill-generation.md) |
| N-8 | Public-corpus introspection ships in v0 with strict privacy, license, and rate-limit constraints; per-source opt-in only | [04](04-code-graph.md), [05](05-pattern-mining.md) |
| N-9 | The Atlas itself ships an entity-level Qdrant collection so the knowledge surface is queryable before pattern mining is online | [06](06-knowledge-index.md) |
| N-10 | LocalCodeGraph v0 ships with Python AST as minimum-viable language coverage; additional languages (TypeScript next) follow phase-by-phase | [04](04-code-graph.md), [12](12-phasing.md) |
| N-11 | PublicCorpusGraph v0 ships with one backend (GitHub Code Search API); additional backends (Sourcegraph public, grep.app) follow as needed | [04](04-code-graph.md), [12](12-phasing.md) |
| N-12 | Discriminator evidence-sufficiency bar is higher for public-corpus-only evidence; public-corpus corroborating ingested-corpus is treated as strong signal | [05](05-pattern-mining.md) |

## Document index

1. [Modules and contracts](01-modules-and-contracts.md) — module map, the
   typed boundaries between them, dispatch and ownership.
2. [Sources](02-sources.md) — `SourceRef`, `Acquirer`, `Parser`, locality
   matrix, version detection.
3. [Atlas](03-atlas.md) — entities, edges, hashing, snapshots, SQLite
   schema.
4. [Code graph](04-code-graph.md) — AST extraction across source kinds,
   reference graph construction, slicing API.
5. [Pattern mining](05-pattern-mining.md) — pattern types, mining
   gradient, LLM synthesis from slices, validation, public-corpus
   subsection (deferred).
6. [Knowledge index](06-knowledge-index.md) — Qdrant collections, hybrid
   retrieval, HyDE, reranking, graph expansion.
7. [Knowledge MCP surface](07-knowledge-mcp-surface.md) — synthetic tools,
   contracts, progressive disclosure, auth pass-through.
8. [Caching and updates](08-caching-and-updates.md) — hash hierarchy,
   surgical updates, change classification, snapshot lifecycle.
9. [Task decomposition](09-task-decomposition.md) — planner as consumer
   of the knowledge surface.
10. [Skill generation](10-skill-generation.md) — skill packages,
    synthesis pipeline, discriminator, feedback loop. Consumer module.
11. [Module layout](11-module-layout.md) — where new code lives in
    `src/mcp_semantic_gateway/`.
12. [Phasing](12-phasing.md) — milestones, exit criteria, ship gate at
    Phase 5.

## What this design intentionally does *not* cover

- **Sandboxed execution validation.** Out of scope; agents verify their
  own results.
- **Auto-repair of skills with breaking-change dependencies.** Quarantine
  is the only safe response.
- **Late-interaction multi-vector retrieval (ColBERT).** Hybrid bi-encoder
  + BM25 + reranker is sufficient for v1.
- **Skill marketplaces, signing, sharing.** Generated skills live in the
  user's project directory.
- **Language parsers beyond Python for `LocalCodeGraph`.** Python AST
  is the v0 minimum; TypeScript is the next addition; everything else
  is on demand.
- **Public-corpus backends beyond GitHub Code Search.** v0 ships one
  backend; Sourcegraph public, grep.app, and Software Heritage are
  added as the demand profile shows.
- **Cross-subject pattern unification.** Patterns are tagged with the
  subjects whose evidence supports them; v0 treats per-subject patterns
  as distinct records (deduplicated only by `pattern_hash`). A future
  unification step that merges semantically equivalent patterns across
  subjects is post-v1.
- **Runtime synthesis on retrieval miss.** The knowledge surface returns
  what it has; gap-driven synthesis is a Phase-7+ flag.
