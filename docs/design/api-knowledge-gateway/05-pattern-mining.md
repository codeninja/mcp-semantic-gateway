# 05 — Pattern Mining

The pattern-mining module converts the Atlas + code graph into
**Patterns**: first-class records describing how entities are used in
practice. Patterns are not generated as a side-effect of skill
synthesis. They are persisted, hashed, embedded, and queryable on their
own.

## Pattern kinds

```python
class PatternKind(Enum):
    CO_OCCURRENCE = "co-occurrence"
    SEQUENCE = "sequence"
    IDIOM = "idiom"
    CONSTRAINT = "constraint"
    USE_CASE = "use-case"
```

| Kind | What it captures | Example (Godot) |
|---|---|---|
| co-occurrence | Entities that appear together in usage | "`RigidBody3D` + `CollisionShape3D` + `SphereShape3D` co-occur in 22/26 mined examples" |
| sequence | Order in which calls happen | "`add_child()` is called before `set_position()` in 18/19 examples" |
| idiom | A reusable code template distilled from many similar examples | "Standard physics-controlled-character idiom: extends RigidBody3D, exposes `_physics_process`, calls `apply_impulse(direction * speed)`" |
| constraint | A precondition or invariant on usage | "`SpringArm3D.add_excluded_object` must be called after the node is in the scene tree" |
| use-case | Real-world task one or more entities support | "Build a ball that rolls under gravity and responds to input" |

The first four are *descriptive*: they describe how the corpus uses the
API. The fifth (use case) is *aspirational*: it describes what an agent
might want to do. The same module produces all five because they share
inputs (slices from the code graph) and outputs (Pattern records in the
Atlas).

## Mining gradient — deterministic before LLM

Decision N-5: pattern miners run in a strict order. Cheaper signal
gates expensive signal.

### Tier 1 — Deterministic miners

Run first, no LLM. Produce co-occurrence and sequence patterns directly
from the code graph.

```python
class CoOccurrenceMiner(PatternMiner):
    determinism = "deterministic"
    strategy = "co-occurrence"

    async def mine(self, snapshot_id: str, *, scope: MiningScope) -> AsyncIterator[Pattern]:
        # For each entity in scope, count co-occurring entities across
        # all examples it appears in. Threshold on minimum co-occurrence
        # count and ratio. Emit one Pattern per significant cluster.
        ...

class SequenceMiner(PatternMiner):
    determinism = "deterministic"
    strategy = "sequence"

    async def mine(self, snapshot_id: str, *, scope: MiningScope) -> AsyncIterator[Pattern]:
        # For each pair (A, B) in co-occurrences, count how often A's
        # reference precedes B's in the same example block. If ratio
        # is high, emit a sequence Pattern (A → B).
        ...
```

These miners read only from the code graph (`SIBLING`, `PRECEDES`,
`CALL` references). No model calls. Cost is dominated by SQL
aggregation.

### Tier 2 — Statistical miners

Run second. Use frequency thresholds and clustering on Tier-1 outputs
to find higher-order patterns. Still no LLM.

```python
class IdiomClusterMiner(PatternMiner):
    determinism = "statistical"
    strategy = "idiom"

    async def mine(self, snapshot_id: str, *, scope: MiningScope) -> AsyncIterator[Pattern]:
        # Cluster examples by structural similarity (entity-set + edge-set).
        # Each cluster of N+ examples is a candidate idiom; emit a
        # Pattern with the cluster's example IDs and a structural
        # template (highest-shared subgraph).
        ...

class ConstraintMiner(PatternMiner):
    determinism = "statistical"
    strategy = "constraint"

    async def mine(self, snapshot_id: str, *, scope: MiningScope) -> AsyncIterator[Pattern]:
        # Pattern-match known constraint phrasings ("must be called from",
        # "only valid in", "deprecated since") against entity docs.
        # Optionally promote sequence patterns with very high consistency
        # (>95%) to constraints.
        ...
```

### Tier 3 — LLM-induced miners

Run last, gated on the validation pass. Take **merged slices across
all enabled code-graph subjects** for each entity (or each cluster of
related entities) and ask an LLM to induce patterns the deterministic
tiers missed.

```python
class LLMPatternMiner(PatternMiner):
    determinism = "llm-induced"
    strategy = "idiom" | "constraint" | "use-case"   # configurable
    requires_local_safe_llm: bool = False             # set True if local-project content is in the slice

    async def mine(self, snapshot_id: str, *, scope: MiningScope) -> AsyncIterator[Pattern]:
        for entity in scope.entities:
            merged = await code_graph_mux.slice_for(
                entity.id,
                budget_tokens=8000,
                subjects=scope.subjects,             # which corpora to draw from
            )
            response = await llm.structured_synthesize(
                prompt=PATTERN_INDUCTION_PROMPT,
                slice=merged,
                schema=PATTERN_OUTPUT_SCHEMA,
            )
            for raw_pattern in response.patterns:
                pattern = compose_pattern(raw_pattern, evidence=merged)
                if await discriminator.validate_pattern(pattern):
                    yield pattern
```

The merged slice **is the prompt context**. This is the mechanism the
user asked for: "look at an unknown method, find its relations in the
code graph, feed those slices to the LLM" — with the graph now spanning
the ingested corpus, the user's project (when enabled), and public
external code (when enabled).

LLM-induced patterns are gated by checks before publish:

1. **Atlas grounding** — every entity referenced in the pattern must
   resolve in the Atlas snapshot.
2. **Evidence sufficiency** — the pattern must cite at least N source
   locations from the slice. The threshold is **subject-aware**
   (decision N-12):

   | Evidence composition | Min citations to publish |
   |---|---|
   | All ingested-corpus | 2 |
   | Mixed (any ingested + any other) | 2 |
   | All local-project | 3 |
   | All public-corpus | 4, AND ≥2 distinct repos, AND ≥2 distinct licenses |

   The higher bar for public-corpus-only evidence reflects its lower
   curation. Public-corpus evidence corroborating ingested-corpus is
   strong signal and gets the standard threshold.

3. **License hygiene** — for any pattern citing public-corpus evidence,
   every cited repo must be on the configured license allowlist. A
   single non-allowlisted citation invalidates the pattern.

A pattern that fails any check is dropped, not retried.

### Per-subject privacy guarantees

When `LocalCodeGraph` is enabled, the LLM-induced miner imposes one
additional constraint:

- **Local-project content never reaches an external LLM provider unless
  the user has marked the source as `local_safe = true`.** The
  miner's prompt either includes local-project slice components (when
  the LLM is local-safe) or omits them (when it is not). The
  discriminator's evidence-sufficiency tally is computed over the
  slice the LLM actually saw, not the full merged slice.

This is enforced at slice-assembly time in the `CodeGraphMux`, not at
the miner level — so consumers can't bypass the rule by ignoring the
flag.

## Use case generation

Use cases are produced by an LLM-induced miner with a different prompt
profile. Where pattern miners ask "what happens here?", the use-case
miner asks "what real-world task could a user accomplish with these
entities?"

```python
class UseCaseMiner(PatternMiner):
    determinism = "llm-induced"
    strategy = "use-case"

    async def mine(self, snapshot_id: str, *, scope: MiningScope) -> AsyncIterator[Pattern]:
        # Cluster the snapshot using parser partition hints + edge-density
        # graph clustering. For each cluster, slice the cluster's entity
        # set and ask the LLM for use cases that the cluster supports.
        ...
```

Use case clustering and prompting follow the structure of the prior
design (see `../api-introspection-skills/05-synthesis-and-validation.md`),
adapted to use the code graph's slices as input rather than entity docs
alone. The richer input materially improves use case specificity.

## Mining scope

```python
class MiningScope(BaseModel):
    snapshot_id: str
    entity_ids: set[str] | None = None        # None = whole snapshot
    partition_groups: set[str] | None = None
    target_kinds: set[PatternKind] | None = None
    subjects: set[CodeGraphSubject] | None = None   # None = all enabled
    budget: MiningBudget | None = None        # token / call caps
```

`subjects` lets a miner restrict to a specific corpus (e.g., run a
deterministic miner over `LOCAL_PROJECT` only to surface idioms
specific to the user's codebase style).

Scope is the lever for surgical updates: when a small set of entities
changes, miners re-run on just that scope (plus the immediate-neighbor
hop) rather than the whole snapshot.

## Pattern store

Patterns persist via the Atlas (`patterns` and `pattern_participants`
tables in [03](03-atlas.md)). The pattern-mining module wraps the Atlas
repository for write semantics:

```python
class PatternStore(Protocol):
    async def write(self, pattern: Pattern) -> None: ...
    async def list_for_entity(self, entity_id: str) -> list[Pattern]: ...
    async def list_by_kind(self, snapshot_id: str, kind: PatternKind) -> Iterable[Pattern]: ...
    async def supersede(self, prior_pattern_id: str, new_pattern_id: str) -> None: ...
```

Patterns can supersede prior patterns within a snapshot lineage. When a
miner produces a pattern that subsumes an earlier one (same
participants, higher confidence), the prior pattern is marked
superseded; queries return only non-superseded patterns by default.

## Pattern record shape

```python
class Pattern(BaseModel):
    id: str                                  # 'pat-<snapshot>-<hash>'
    snapshot_id: str
    kind: PatternKind
    determinism: Literal["deterministic", "statistical", "llm-induced"]
    description: str                         # human-readable summary
    participants: list[Participant]
    body: PatternBody                        # kind-specific structured body
    evidence: list[Evidence]                 # SourceLocation refs
    confidence: float
    discriminator_passes: list[str]
    pattern_hash: str
    generated_at: datetime
    superseded_by: str | None = None

class Participant(BaseModel):
    entity_id: str
    role: Literal["subject", "collaborator", "precondition", "postcondition"]

class Evidence(BaseModel):
    file_path: str
    line_start: int
    line_end: int
    excerpt: str
```

`PatternBody` is a discriminated union by `kind`:

- `CoOccurrenceBody` — entity multiset, count, ratio, example IDs.
- `SequenceBody` — ordered participant list, observed transition counts.
- `IdiomBody` — code template with placeholders, parameter spec.
- `ConstraintBody` — natural language statement + machine-readable
  predicate when extractable.
- `UseCaseBody` — task description, target entity set, prerequisites,
  outcomes.

## Validation

Patterns flow through a discriminator (shared with skill-gen, see
[10](10-skill-generation.md)). Pattern-specific passes:

| Pass | Applies to | What it checks |
|---|---|---|
| Spec | all | Required fields present; participants non-empty; hash matches |
| Atlas grounding | all | Every cited entity resolves in `snapshot_id` |
| Evidence sufficiency | LLM-induced | At least N evidence citations from the slice |
| Coherence | all | `participants` ⊆ entities cited in `evidence` |
| Determinism-tier | LLM-induced | Pattern doesn't restate a high-confidence deterministic pattern verbatim |

A failed pattern is recorded with `status='draft'` and surfaced in
diagnostics, not published.

## Public-corpus introspection (active in v0)

Decision N-8: public-corpus introspection ships in v0 with strict
constraints; per-source opt-in only. The implementation lives in
[04-code-graph.md](04-code-graph.md) under `PublicCorpusGraph`; the
pattern-mining concerns are summarized here.

Pattern miners receive merged slices combining ingested-corpus,
local-project, and public-corpus references. Each evidence entry on
a `Pattern` carries the `subject` it came from plus full provenance.

Constraints, all enforced before a public-corpus pattern can publish:

- **Privacy** — never enabled by default. Per-source opt-in flag.
  Public-corpus mining never runs against private sources by accident.
- **Rate limiting** — enforced at the acquirer ([02](02-sources.md)).
  Default: 1 query/second/backend, 1000 queries/snapshot.
- **Provenance** — every evidence citation includes repo, commit SHA,
  path, license, star count.
- **License hygiene** — license allowlist per source. Default:
  permissive only (MIT, Apache-2.0, BSD-2/3-Clause, MPL-2.0).
- **Discriminator stricter** — evidence-sufficiency threshold for
  public-corpus-only evidence requires ≥4 citations from ≥2 distinct
  repos with ≥2 distinct licenses (see Tier 3 above).

### Targeted public-corpus mining

Full-sweep public-corpus mining is expensive. v0 also ships a
**targeted mode** where a Tier-2 pattern with marginal confidence
triggers a single targeted public-corpus query for corroboration:

```python
class CorroborationMiner(PatternMiner):
    determinism = "statistical"
    strategy = "co-occurrence" | "sequence" | "idiom"

    async def mine(self, snapshot_id: str, *, scope: MiningScope) -> AsyncIterator[Pattern]:
        for marginal_pattern in await store.list_marginal(snapshot_id):
            external_evidence = await public_corpus_graph.query_for_pattern(marginal_pattern)
            if external_evidence.corroborates:
                yield marginal_pattern.with_added_evidence(external_evidence)
```

This converts the public-corpus from a primary mining input into a
**confidence amplifier** for ingested-corpus patterns — the highest-
ROI use of external evidence.

## Cost control

- Tier 1 + Tier 2 are fast (SQL + tokenization). Run on every snapshot
  unconditionally.
- Tier 3 (LLM) is metered. Per-snapshot LLM budget is configurable;
  the pipeline records token spend per miner per snapshot.
- LLM-induced mining caches per-entity slice → pattern by `slice_hash`.
  Re-running on an unchanged snapshot is free.

## What pattern-mining does NOT do

- It does not produce skills. Skills are downstream consumers
  ([10](10-skill-generation.md)).
- It does not change the Atlas schema. New `EdgeKind` values are
  declared in [03](03-atlas.md) and produced by parsers/code-graph
  builders.
- It does not gate on retrieval fitness. That is a knowledge-index
  concern.

## Open implementation choices

- **Idiom template extraction.** Producing a parameterized code
  template from a cluster of examples is a non-trivial structural
  generalization step. v1 ships a conservative template (LCS over
  AST shapes); LLM-assisted templating is Phase-4+.
- **Statistical thresholds.** Co-occurrence count / ratio thresholds
  are placeholders. Calibrate on Godot data before locking defaults.
- **Cross-source patterns.** A pattern that spans entities from two
  different sources (e.g., an OpenAPI client + a database client) is
  not modeled in v1. Atlas already lacks cross-source edges; pattern
  mining inherits the limitation.
