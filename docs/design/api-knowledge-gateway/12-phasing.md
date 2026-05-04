# 12 — Phasing

This is the proposed delivery order. Each phase has explicit exit
criteria. Order is chosen to maximize early shippable value: the
**knowledge surface is in production by Phase 5**, before any pattern
mining or skill generation.

The Godot proof-of-concept (decision I-14) is the integration target
across phases. OpenAPI is the simpler ground-truth source used in
earlier phases to move quickly on foundations.

## Phase 0 — Foundations

**Goal:** data model, storage, and project layout in place. No new
agent-visible behavior yet.

**Scope:**
- `storage/`: project-local `.mcp_semantic_gateway/` layout, SQLite
  migrations, Qdrant client wrapper.
- `atlas/`: Entity, Edge, AtlasSnapshot models; hashing; repository.
- `config/`: `SourceConfig` polymorphism, `IndexConfig`.
- Replace `storage/vector_store.py` with Qdrant-backed implementation.
  Existing tool/prompt indexing migrates onto Qdrant with no behavior
  change.

**Exit criteria:**
- All existing tests pass against Qdrant (embedded mode).
- Atlas snapshot round-trips through SQLite; hashes are stable.
- `mcp-semantic-gateway init` produces the new project-local
  directory.

**Risk:** Qdrant migration breaks existing integration. Mitigate with
Protocol-shaped wrapper.

## Phase 1 — Sources (acquirers + parsers)

**Goal:** ingest produces Atlas snapshots from local + remote sources.

**Scope:**
- `sources/acquirers/`: `LocalPathAcquirer`, `HTTPAcquirer`,
  `GitAcquirer`.
- `sources/parsers/openapi.py` (rebuilt from `forge.py`).
- `sources/parsers/godot_xml.py` (new).
- `sources/parsers/markdown.py` (basic).
- `sources/pipeline.py` orchestrator.
- Atlas writes from parser output.

**Exit criteria:**
- OpenAPI parser round-trips a non-trivial spec (existing weather
  example) with no entity loss; works against both URL and local
  path acquirers.
- Godot parser ingests Godot 4.4 stable from a git ref AND a local
  checkout; produces the same `snapshot_hash` from both.
- Contract test `test_composition.py` covers every supported
  (acquirer, parser) combination.

**Risk:** Godot XML edge cases (operator overloads, virtual methods,
inherited overrides). Budget a week for the long tail.

## Phase 2 — Knowledge index (entity surface ships)

**Goal:** the gateway is queryable. End users can ask the knowledge
gateway questions about ingested APIs and get entity hits.

**Scope:**
- `knowledge_index/embeddings.py` over the entity collection.
- `knowledge_index/service.py` with hybrid retrieval + filters +
  optional HyDE.
- `knowledge_index/reranker.py` (optional cross-encoder).
- `knowledge_mcp/tools.py`: `query_api`, `describe_entity`, `expand`
  (the three tools that don't need patterns).
- `integration/proxy.py` registers the knowledge tools.

**Exit criteria:**
- `mcp-semantic-gateway query "RigidBody3D"` returns the entity with
  full neighborhood from `describe_entity`.
- An MCP client can call `query_api` against a configured Godot
  source and receive ranked entity hits.
- Latency budget: p95 < 800ms for `query_api` with HyDE on local
  embedded Qdrant.

**At end of Phase 2: the knowledge gateway is in production use.**
Skills, patterns, and the planner are not yet online. The product is
real and shippable.

**Risk:** retrieval quality is mediocre on its own — entities alone
without patterns may answer questions thinly. Acceptable; quality
ratchets up in Phase 4.

## Phase 3 — Code graph (all three subjects)

**Goal:** for every entity in the Atlas, slicing produces a token-
budgeted relevant context drawn from any combination of three corpora:
the ingested source, the user's local project, and external public
code (when enabled).

This phase is intentionally larger than a single subject because the
multi-subject machinery (mux, merged slices, per-subject invalidation)
is load-bearing for downstream pattern mining.

**Scope:**
- `code_graph/builder.py` and `builders/` for ingested subject.
- `code_graph/repository.py` (SQLite tables, multi-subject keyed).
- `code_graph/slicer.py` with budget-aware assembly.
- `code_graph/mux.py` (`CodeGraphMux` + `MergedSlice`).
- `code_graph/subjects/ingested.py`.
- `code_graph/subjects/local_project.py` with
  `code_graph/language_ast/python.py` for v0.
- `code_graph/subjects/public_corpus.py` with
  `code_graph/public_backends/github_code_search.py`.
- `sources/acquirers/local_project.py`.
- `sources/acquirers/github_code_search.py`.

**Exit criteria — `IngestedCodeGraph`:**
- For 20 hand-curated Godot entities, `slice_for(entity_id,
  budget=8000)` returns a non-empty Slice including definition,
  examples, and at least one neighbor.
- Code-graph tables index correctly; reference lookup is sub-100ms
  per entity.
- A regression test verifies the slice is deterministic given the
  same snapshot.

**Exit criteria — `LocalCodeGraph` (v0 Python):**
- Given a fixture Python project that imports a Python source we have
  ingested (e.g., a small wrapper around the OpenAPI weather example),
  `LocalCodeGraph.references_to(entity_id)` returns the expected
  call/import locations.
- Per-source priority resolution: a reference that could match
  entities in two configured sources resolves against the first by
  priority order.
- Privacy guarantee verified: no local-project content reaches an
  external LLM provider when the source is not flagged
  `local_safe = true`.

**Exit criteria — `PublicCorpusGraph` (v0 GitHub Code Search):**
- With a configured `GITHUB_TOKEN`, `PublicCorpusGraph.references_to(entity_id)`
  for a popular API entity returns at least 3 hits, all from
  permissively-licensed repos.
- Rate-limit budget enforced: a synthetic 100-query test does not
  exceed `rate_limit_qps`.
- License-allowlist enforced: a synthetic test where the API returns
  GPL-licensed hits drops them.
- TTL caching verified: a re-run within TTL is a cache hit (no API
  calls).

**Exit criteria — `CodeGraphMux`:**
- A merged slice for an entity with all three subjects enabled
  contains components from each subject in the configured token
  ratio (50/30/20).
- Removing one subject from the enabled set produces a slice with
  rebalanced budgets and no missing-subject content.

**Risk:** symbol resolution accuracy across subjects. Local-project
and public-corpus reference resolution is best-effort; the long tail
needs iteration.

**Risk:** public-corpus cost. A naive sweep can spend rate-limit
budget on entities no one queries. Mitigate by defaulting to the
**targeted** corroboration mode (mining triggered by marginal
patterns in Phase 4) rather than full sweep.

## Phase 4 — Pattern mining (Tier 1 + Tier 2; multi-subject)

**Goal:** deterministic and statistical patterns flow into the
knowledge surface. Patterns can carry evidence from any combination
of enabled code-graph subjects.

**Scope:**
- `pattern_mining/miners/co_occurrence.py`,
  `miners/sequence.py`, `miners/idiom_cluster.py`,
  `miners/constraint.py`.
- `pattern_mining/miners/corroboration.py` — targeted public-corpus
  mining triggered by marginal Tier-2 patterns.
- `pattern_mining/store.py` (`patterns` and `pattern_participants`
  tables).
- Pattern Qdrant collection + payload indexes.
- Update `knowledge_mcp/tools.py` to add `find_patterns` and to
  include patterns in `query_api` results.
- New tool: `mcp_semantic_gateway_local_usage`.

**Exit criteria:**
- For Godot 4.4, ≥200 deterministic patterns and ≥50 statistical
  idioms are produced in a single ingest from `IngestedCodeGraph`
  alone.
- For a configured Python project that uses the ingested OpenAPI
  source, the deterministic miners produce at least 5 local-project-
  specific patterns visible via `find_patterns` with
  `subjects=["local-project"]`.
- With public-corpus enabled, the corroboration miner upgrades at
  least 10% of marginal Tier-2 patterns by adding public-corpus
  evidence; the upgrade is visible in `evidence_subjects`.
- `query_api` for a physics-related query returns at least one
  high-confidence pattern result.
- Re-ingest on the same snapshot produces zero new patterns (cache
  hit on `pattern_hash`).
- Pattern grounding pass runs as part of the discriminator.

**Risk:** statistical thresholds are uncalibrated. Plan for tuning
on Godot data and on local-project fixtures.

**Risk:** public-corpus mining cost. The corroboration miner is the
primary public-corpus consumer; full-sweep public-corpus mining is
opt-in only.

## Phase 5 — Use cases + LLM-induced patterns (gated; multi-subject slices)

**Goal:** complete the knowledge surface with LLM-induced patterns and
use cases driven by **merged slices** across enabled code-graph
subjects; HyDE quality measurably improves.

**Scope:**
- `pattern_mining/miners/llm_pattern.py`,
  `miners/use_case.py`.
- `pattern_mining/llm_client.py`, `prompts.py`.
- `validation/discriminator.py` running grounding + evidence-
  sufficiency on every LLM-induced pattern; subject-aware thresholds
  per N-12.
- Use cases land in their own Qdrant collection; `query_api` and
  `find_patterns` consume them.

**Exit criteria:**
- For Godot 4.4, 100–500 use cases produced spanning all major
  partition clusters.
- Sample 20 LLM-induced patterns: ≥80% pass human spot-check.
- Discriminator drops ≥95% of hallucinated patterns (negative test
  with seeded hallucinations).
- Subject-aware evidence-sufficiency verified: a synthetic test
  generates a "pattern" backed only by 1 public-corpus citation and
  is dropped; the same pattern with 4 public-corpus citations from
  3 distinct repos is accepted.
- Privacy guarantee verified end-to-end: when the local source is
  not flagged `local_safe = true`, no local-project slice content
  reaches the external LLM provider (recorded prompts inspected).
- Re-running on unchanged snapshot is a slice-cache hit (zero LLM
  spend).

**Risk:** LLM hallucination. The discriminator is the single most
load-bearing component; budget extra iteration time.

**Risk:** public-corpus quality variance. Some popular APIs have
many low-quality public uses. The license + star floor + multi-repo
requirements address this; calibration during this phase.

## Phase 6 — Planner (task decomposition)

**Goal:** agents can issue a task and receive a plan with
knowledge-binding sub-goals.

**Scope:**
- `task_decomp/planner.py`, `decomposer.py`, `binder.py`.
- `mcp_semantic_gateway_plan` tool registration.
- CLI `mcp-semantic-gateway plan "<task>"`.

**Exit criteria:**
- Five hand-curated test tasks against Godot produce sensible
  decompositions with knowledge bindings or gap signals.
- Plan cache invalidates correctly on snapshot rotation.
- Plans against multi-source configurations bind correctly.

**Risk:** decomposition quality is LLM-bounded. Calibrate prompts.

## Phase 7 — Skill generation (consumer module)

**Goal:** skill packages can be synthesized from use cases + patterns
and consumed by agents.

**Scope:**
- `skill_gen/synthesizer.py`, `package_builder.py`,
  `description_optimizer.py`, `feedback_aggregator.py`.
- `validation/pattern_attribution.py` (new discriminator pass).
- `skill_gen/retrieval.py` exposes `SkillRetrieval` to the planner.
- Skill MCP tools: `get_skill`, `submit_feedback`, `rollback`.

**Exit criteria:**
- For 10 curated Godot use cases, the pipeline produces published
  skill packages that pass all discriminator passes.
- A negative test: synthesizer that hallucinates a method is caught
  by Atlas grounding.
- The planner, when skill-gen is enabled, returns `skill_binding`
  for sub-goals where a confident skill exists.

**Risk:** skill quality is the highest-uncertainty surface. Static
discriminator carries the load; LLM-judge later.

## Phase 8 — Updates and surgical re-generation

**Goal:** the pipeline is affordable for ongoing maintenance.

**Scope:**
- `updates/pipeline.py`: full surgical / quarantine / regenerate
  decision tree across entities, patterns, and skills.
- Per-parser `classify_entity_change()` rules refined against real
  diffs.

**Exit criteria:**
- Synthetic minor bump (modify 5% of Godot entities) yields ≥90%
  fast-forward and ≥0% quarantine on test artifacts.
- Synthetic breaking change (rename a method) correctly quarantines
  exactly the dependent patterns and skills.
- Negative feedback on a section triggers surgical re-synthesis after
  threshold is reached.

**Risk:** real major bumps (Godot 4 → 5) won't happen during dev;
test with synthetic diffs.

## Out-of-band: deferred capabilities

Tracked but not phased. Pulled in when demand appears.

- **Additional LocalCodeGraph languages** — TypeScript is the
  next-priority addition (likely Phase 3.5 if Godot phasing has
  slack); Go, Rust, Java follow as use cases appear.
- **Additional PublicCorpusGraph backends** — Sourcegraph public,
  grep.app, Software Heritage. Each is a `PublicCorpusBackend`
  implementation; the rest of the pipeline is unchanged.
- **Cross-subject pattern unification** — merging semantically
  equivalent patterns mined separately from different subjects into
  a single record with combined evidence.
- **LLM-judge discriminator pass.** Sampled, not universal. Records to
  diagnostic; gates skills only when telemetry shows static passes
  miss real issues.
- **Runtime synthesis on planner gap.** Bounded budget; keep flag-gated.
- **Streaming planner output.**
- **Cross-source dependency tracking** (a pattern/skill that spans
  two sources).
- **Non-Anthropic LLM providers.**

## Cross-cutting concerns tracked across phases

- **LLM cost observability.** Every LLM call records tokens in/out
  and the originating phase/module. Surfaced via
  `mcp-semantic-gateway stats`. Implemented in Phase 5 alongside
  `pattern_mining/llm_client.py`.
- **Test fixtures.** Cassette-style recorded LLM responses. Updates
  gated on a flag.
- **Telemetry.** Per-module counts and durations. Logs only in v1.
- **Documentation.** Each phase ships with a `docs/guide/` entry.

## What this design explicitly defers past v1

- LocalCodeGraph languages beyond Python (TypeScript is next).
- PublicCorpusGraph backends beyond GitHub Code Search.
- Cross-subject pattern unification.
- Multi-vector / late-interaction retrieval (ColBERT).
- Cross-source dependency tracking.
- Skill marketplaces, signing, sharing.
- Auto-repair of breaking-change-affected skills.
- Sandboxed execution validation.
- Streaming planner output.
- Non-Anthropic LLM providers.

Each is plausibly valuable. None is on the critical path for the v1
ship gate at end of Phase 5 (knowledge surface — including all three
code-graph subjects — in production).
