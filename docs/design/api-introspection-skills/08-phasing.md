# 08 — Phasing and Milestones

This is the proposed delivery order. Each phase has explicit exit criteria.
Order minimizes wasted work: shared foundations come first; the components
most exposed to user-facing risk come last.

The Godot proof-of-concept (decision 17) is the integration target. It is
not the *first* milestone — earlier phases use OpenAPI as a simpler
ground-truth source so we can move quickly on the foundations.

## Phase 0 — Foundations

**Goal:** data model and storage in place. No new agent-visible behavior
yet.

**Scope:**
- `atlas/` package: `Entity`, `Edge`, `AtlasSnapshot`, hashing, SQLite
  repository.
- `adapters/base.py`: `SourceAdapter` Protocol and `RawSnapshot`.
- `adapters/registry.py`.
- New SQLite tables per [03](03-storage-and-retrieval.md).
- Replace `storage/vector_store.py` with the Qdrant-backed
  implementation. Existing tool/prompt indexing migrates onto Qdrant
  with no behavior change.
- Project-local `.mcp_semantic_gateway/` directory layout per [02](02-skill-packages.md).

**Exit criteria:**
- All existing tests pass against Qdrant (embedded mode).
- A unit test round-trips an Atlas snapshot through SQLite and verifies
  hashes are stable.
- `mcp-semantic-gateway init` produces the new project-local directory.

**Risk:** Qdrant migration breaks the existing integration. Mitigate
with a thin Protocol-shaped wrapper so callers don't change.

## Phase 1 — Source adapters

**Goal:** ingest produces Atlas snapshots from real sources. No skills
yet.

**Scope:**
- `adapters/openapi/`: rebuilt from `ingestion/forge.py`. Adds `$ref`
  resolution, security schemes, examples, partition hints by tag.
- `adapters/godot_xml/`: new. Parses `doc/classes/*.xml` from a Godot
  engine checkout. Partition by class.
- Both adapters implement: `acquire`, `parse`, `partition_hint`,
  `detect_version`, `classify_entity_change`, `tool_requirements`,
  `extract_symbols`.
- `atlas/` repository writes from adapter output.

**Exit criteria:**
- OpenAPI adapter ingests a non-trivial spec (e.g., the existing weather
  example) and round-trips through the Atlas with no entity loss.
- Godot adapter ingests Godot 4.4 stable and produces an Atlas with
  expected entity counts (~1500 classes; methods/properties in the tens
  of thousands).
- Two-adapter coverage validates that the `SourceAdapter` Protocol is
  general enough.

**Risk:** Godot XML edge cases (operator overloads, virtual methods,
inherited overrides). Budget a week for the long tail.

## Phase 2 — Use case generation

**Goal:** Atlas snapshots produce a usable use-case corpus.

**Scope:**
- `ingestion/use_cases.py`: cluster-aware generation pipeline.
- `synthesis/llm_client.py`: Anthropic SDK integration.
- `synthesis/prompts.py`: prompt templates for use-case generation.
- Use cases land in SQLite + Qdrant `use_cases` collection.

**Exit criteria:**
- For Godot 4.4, the pipeline produces between 100 and 500 use cases
  spanning all major partition clusters (physics, rendering, input, UI,
  audio, etc.).
- A sample of 20 generated use cases is human-reviewed and ≥80% are
  judged "well-formed and realistic."
- Re-running on the same snapshot produces no new use cases (cache hit).

**Risk:** Generated use cases are vague or parochial. Mitigate with
tighter prompt rubrics and cluster-level (not entity-level) generation.

## Phase 3 — Skill synthesis + static discriminator

**Goal:** end-to-end pipeline produces published skill packages.

**Scope:**
- `synthesis/skill_synthesizer.py` (and dependents).
- `synthesis/description_optimizer.py` (HyDE rewrite of frontmatter
  description).
- `validation/`: all four static discriminator passes.
- `storage/package_store.py`: write/read skill packages.
- `ingestion/index_writer.py`: orchestrate use-case → synthesis →
  discriminator → publish flow.

**Exit criteria:**
- For a curated subset of 10 Godot use cases (mix of physics, input,
  rendering), the pipeline produces published skill packages that pass
  all discriminator passes.
- A generated skill, loaded by an agent, contains correct API references
  (validated against Atlas grounding) and runnable example code that
  uses real Godot 4.4 APIs.
- A negative test: a synthesizer that hallucinates a method is caught
  by Pass 2 and the skill is blocked from publish.

**Risk:** This is the highest-uncertainty phase. The discriminator is
what makes generated content trustworthy — its passes need real teeth.
Plan for extra iteration time on Pass 2 (Atlas grounding) — the
adapter-supplied symbol parser is non-trivial per source.

## Phase 4 — Planner and MCP surface

**Goal:** agents can use the system end-to-end.

**Scope:**
- `planning/`: planner, decomposer, binder, plan cache.
- `retrieval/service.py` and components (HyDE, reranker, graph expander).
- `integration/proxy.py`: four new synthetic tools.
- `integration/server.py`: HTTP mirrors.
- CLI subcommands: `plan`, `feedback`, `rollback`.

**Exit criteria:**
- An agent can issue a Godot task (e.g., "build a third-person camera"),
  receive a plan, and the plan binds skills for the bindable sub-goals
  and emits gaps for the rest.
- Proxy tests cover the new synthetic tools.
- `mcp-semantic-gateway plan "..."` from the CLI produces sensible plans
  for at least 5 hand-curated test tasks.

**Risk:** Decomposition quality is LLM-quality-bounded. Calibrate
prompts; budget LLM iteration.

## Phase 5 — Caching, surgical updates, feedback aggregation

**Goal:** the pipeline is affordable for ongoing maintenance.

**Scope:**
- `ingestion/update_pipeline.py`: full fast-forward / surgical /
  quarantine / regenerate decision tree.
- `synthesis/feedback_aggregator.py`: rollups, threshold triggers.
- Per-Source-Adapter `classify_entity_change()` rules.

**Exit criteria:**
- Re-ingest of a synthetic minor bump (modify 5% of Godot entities)
  produces ≥90% fast-forward and ≥0% quarantine on the test skill set.
- Re-ingest of a synthetic breaking change (rename a method) correctly
  quarantines exactly the dependent skills.
- Feedback submitted on a skill section triggers surgical re-synthesis
  of that section after the threshold is reached.

**Risk:** Real major bumps (Godot 4 → 5) won't happen during
development; we have to test with synthetic diffs. Acceptable; revisit
when a real major lands.

## Phase 6 — Optional: runtime synthesis on gap

**Goal:** when the planner emits a gap, the system can attempt to
synthesize a skill on demand.

**Scope:**
- Wire `synthesis_on_gap` flag in the planner.
- Bounded synthesis budget per request.
- Caching: synthesized skills land in storage like batch-generated ones,
  available for future queries.

**Exit criteria:**
- A query that produces a gap, with the flag enabled, results in a
  newly published skill within the synthesis budget.
- The newly published skill passes the static discriminator (or is
  rejected with a clear reason).
- Latency budget is respected; gap-only fallback works when budget
  exhausts.

**Risk:** Runtime synthesis in the retrieval hot path is the riskiest
piece. Keep it flag-gated and per-tenant rate-limited.

## Out-of-band: LLM-judge discriminator

A future enhancement (no fixed phase). Adds Pass 5 to the discriminator:
an LLM judge that asks "would this skill, followed correctly, accomplish
the use case?" Sampled, not universal. Records into `.meta.json`. Doesn't
gate publication in v1; may gate in v2.

## Out-of-band: additional Source Adapters

After Godot proves the model, plausible follow-ups in priority order:

1. Sphinx-generated Python doc sites (pandas, requests, scikit-learn).
2. TypeScript `.d.ts` declaration parsing.
3. LSP symbol dumps (any language with an LSP).
4. JSDoc / TSDoc.
5. Doc-site adapters for sites without a structured source (HTML +
   LLM-assisted extraction).

Each is a self-contained `adapters/<kind>/` subpackage. The rest of the
pipeline doesn't change.

## Cross-cutting concerns tracked across phases

- **LLM cost observability.** Every LLM call records tokens in/out and
  associated phase. Surfaced via `mcp-semantic-gateway atlas stats`.
  Implemented in Phase 2 alongside `llm_client.py`.
- **Test fixtures.** Cassette-style recorded LLM responses for every
  fixture-using test. Updating the cassettes is a deliberate dev-time
  step gated on a flag.
- **Telemetry / metrics.** Pipeline emits per-phase counts and durations.
  No external sink in v1; logs are sufficient.
- **Documentation.** Each phase ships with a guide doc in `docs/guide.md`
  or a sibling so users can drive it.

## What we explicitly defer past v1

- Multi-vector / late-interaction retrieval (ColBERT-style).
- Cross-source dependency tracking.
- Skill marketplaces, signing, sharing.
- Auto-repair of skills with breaking-change dependencies.
- Sandboxed execution validation (headless engines, etc.).
- Streaming planner output.
- Non-Anthropic LLM providers.

Each is plausibly valuable. None is on the critical path for shipping
the feature described here.
