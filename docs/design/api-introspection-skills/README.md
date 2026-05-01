# API Introspection & Generated Skills — Design

This directory holds the design for a major capability addition to the MCP
Semantic Gateway: ingesting third-party APIs (REST, class-based, doc-site, or
otherwise), deriving real-world use cases from them, synthesizing
agent-skills-spec-conformant skill packages that satisfy those use cases, and
serving them through the existing semantic gateway with planning, feedback,
versioning, and surgical updates.

These documents describe the *intended* design. Nothing in this directory has
been implemented. They exist to be argued with before code lands.

## Vision

The current gateway is a retrieval surface over hand-authored tools, prompts,
and skills. This feature extends it on the ingest side: pointed at an API
documentation source, the gateway should produce a curated library of skill
packages that an agent can discover and execute against the underlying API
through the gateway. The agent observes its own results (rendering, side
effects, return values); we are a capability provider, not a correctness
oracle.

The proof-of-concept target is the Godot engine — its public class reference
is large, deeply structured, non-OpenAPI, and exercises every hard part of the
pipeline (non-uniform docs, class hierarchies, signals, lifecycle hooks,
thousands of properties).

## Glossary

- **Source** — A third-party API we ingest. Examples: an OpenAPI spec, the
  Godot engine's XML class reference, a Sphinx-generated doc site.
- **Source Adapter** — A module that knows how to acquire and parse one kind
  of source into the unified Atlas representation.
- **Atlas** — The unified, source-agnostic representation of an ingested API.
  A graph of typed entities (classes, methods, endpoints, properties, signals,
  enums, etc.) with relationships (extends, emits, calls, requires).
- **Atlas Snapshot** — A frozen, content-hashed Atlas for a specific source
  version. Skills are pinned to snapshots.
- **Use Case** — A synthesized description of a real-world task that one or
  more Atlas entities support. Use cases are first-class records with their
  own embeddings.
- **Skill Package** — A directory containing a `SKILL.md` (per the agent-skills
  spec), a `.meta.json` sidecar, and supporting `references/`, `examples/`,
  and optional `scripts/`.
- **Section** — A unit of a skill package whose dependencies are tracked
  separately for surgical updates. Section boundaries are derived from the
  Source Adapter's partition hint (e.g., per-class for Godot, per-resource
  for OpenAPI).
- **Planner** — The new gateway-side component that decomposes an agent task
  into sub-goals and binds each sub-goal to a skill (or returns a gap).
- **Discriminator** — Static validator that gates publication of generated
  skills. Checks spec conformance, Atlas grounding, internal coherence, and
  retrieval-fitness.

## Decisions log

This is the running list of design decisions made during planning. Each is
elaborated in the linked doc.

| # | Decision | Doc |
|---|---|---|
| 1 | Skill format follows the agent-skills specification (SKILL.md + supporting files) | [02](02-skill-packages.md) |
| 2 | Skills stored under `.mcp_semantic_gateway/skills/<source>/<source-version>/<skill-id>/v<n>/` | [02](02-skill-packages.md) |
| 3 | `.meta.json` sidecar carries provenance, atlas dependencies, sections, lineage, feedback | [02](02-skill-packages.md) |
| 4 | Atlas is unified across source types; Source Adapters normalize | [01](01-atlas.md) |
| 5 | Atlas versioning is per-source (one snapshot per source version) | [01](01-atlas.md) |
| 6 | Section granularity is auto-derived from Source Adapter partition hints | [01](01-atlas.md), [02](02-skill-packages.md) |
| 7 | Static discriminator for v1; LLM-judge planned later | [05](05-synthesis-and-validation.md) |
| 8 | Skill bodies are spec-conformant procedural prose; structure lives in `.meta.json` | [02](02-skill-packages.md) |
| 9 | Planner lives in this codebase (`mcp_semantic_gateway_plan` synthetic tool) | [04](04-planner.md) |
| 10 | Vector store migrates from hnswlib to **Qdrant** (embedded for dev, server for prod) | [03](03-storage-and-retrieval.md) |
| 11 | Auth passes through from MCP source config; skills reference auth by name | [03](03-storage-and-retrieval.md) |
| 12 | `allowed-tools` derived automatically from Atlas entities a skill references | [02](02-skill-packages.md) |
| 13 | Feedback is the refinement signal; aggregated at `(source, major, skill_id)`; resets on major bump | [05](05-synthesis-and-validation.md), [06](06-caching-and-updates.md) |
| 14 | Cross-major source bumps regenerate skills cold; minor/patch use surgical updates | [06](06-caching-and-updates.md) |
| 15 | Cross-major feedback survives only as soft prior to the synthesizer, not as ranking signal | [05](05-synthesis-and-validation.md) |
| 16 | Breaking changes auto-quarantine affected skills; no auto-repair | [06](06-caching-and-updates.md) |
| 17 | First non-OpenAPI proof-of-concept is Godot (hard mode by request) | [08](08-phasing.md) |
| 18 | Project-local data directory is `.mcp_semantic_gateway/` | [02](02-skill-packages.md) |

## Document index

1. [Atlas — schema, source adapters, partition hints, hashing](01-atlas.md)
2. [Skill packages — on-disk format, `.meta.json`, sections, lineage](02-skill-packages.md)
3. [Storage & retrieval — Qdrant, payload filters, hybrid search](03-storage-and-retrieval.md)
4. [Planner & MCP surface — `mcp_semantic_gateway_plan` and friends](04-planner.md)
5. [Synthesis & validation — use cases, skill generation, discriminator, feedback](05-synthesis-and-validation.md)
6. [Caching & updates — surgical updates, quarantine, cross-major resets](06-caching-and-updates.md)
7. [Module layout — where new code lives in `src/`](07-module-layout.md)
8. [Phasing — milestones and exit criteria](08-phasing.md)

## What this design intentionally does *not* cover

- Sandboxed execution validation (headless Godot, etc.). The agent verifies
  its own actions; we provide capability.
- Generated-skill auto-repair on breaking changes. Quarantine is the only
  safe response in v1.
- A multi-vector / late-interaction retrieval layer (ColBERT-style). Hybrid
  bi-encoder + BM25 + reranker is sufficient for v1.
- Runtime skill synthesis on retrieval miss. The planner returns a gap; an
  optional flagged Phase-6 capability may add this later.
- Skill marketplaces, sharing, or signing. Generated skills live in the
  user's project directory; sharing is out of scope.
