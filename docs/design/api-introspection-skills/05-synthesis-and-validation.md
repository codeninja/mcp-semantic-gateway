# 05 — Synthesis and Validation

Three pipelines turn an Atlas snapshot into published skills:

1. **Use-case generation** — derives use cases from Atlas regions.
2. **Skill synthesis** — produces a skill package for each (use case, atlas)
   pair.
3. **Discrimination** — validates the synthesized skill before publish.

Plus a feedback loop that triggers re-synthesis when accumulated negative
feedback warrants it.

## Use-case generation

### Why use cases are first-class

A use case is not an ephemeral artifact of skill generation — it is a
persistent record. Use cases:

- Index into Qdrant alongside skills, so retrieval can match an agent's
  query to a use case even when no skill exists yet (gap signal).
- Persist across skill regenerations within a major version, so feedback
  on a use case ("this use case is poorly described / missing / wrong") is
  separate from feedback on a particular skill version.
- Cluster naturally — use cases generated for "physics" don't need to be
  regenerated when "rendering" classes change.

### Generation strategy

Cluster-aware, not entity-aware. Generating one use case per Atlas entity
explodes token cost and produces parochial use cases. Instead:

1. **Cluster the Atlas.** Use the Source Adapter's partition hint as a
   starting point, then merge/split clusters using a graph-based grouping
   over Atlas edges (entities densely connected by `extends`, `contains`,
   `emits` end up in the same cluster).
2. **For each cluster, prompt an LLM** with the cluster's entity catalog
   (qualified names + short docs) and ask for a list of real-world use
   cases the cluster's entities support. Output is structured.
3. **Deduplicate across clusters.** A use case may legitimately span
   multiple clusters (e.g., "third-person camera with input-driven
   rotation" spans camera + input). Cross-cluster dedup uses semantic
   similarity over use case descriptions.
4. **Persist** to `use_cases` table; embed and add to Qdrant `use_cases`
   collection.

### Use case record

```json
{
  "id": "uc-godot-physics-rolling-ball",
  "snapshot_id": "snap-7f3a...",
  "description": "Build a rigid-body sphere that rolls under gravity, responds to impulses, and collides with static geometry.",
  "cluster_id": "cluster:physics",
  "linked_entities": [
    "godot.RigidBody3D", "godot.SphereShape3D", "godot.CollisionShape3D",
    "godot.Node._physics_process", "godot.Vector3.UP"
  ],
  "confidence": 0.82,
  "generated_by": "use-case-gen@0.1.0"
}
```

### Cost control

Generation runs once per Atlas snapshot. Surgical updates (see [06](06-caching-and-updates.md))
re-generate only use cases whose `linked_entities` set has changed
materially. Caching keys on `cluster_hash = hash(sorted(entity_hashes_in_cluster))`.

## Skill synthesis

For each use case (or pull request from the feedback loop), the synthesizer
produces a skill package.

### Inputs

- A target **use case** (or a use-case-shaped feedback note).
- The relevant **Atlas region** — entities reachable from the use case's
  `linked_entities` plus a graph-expansion hop.
- The Source Adapter's **partition hints** for that region (drives section
  layout).
- Optional **carry-forward priors** — if regenerating after a major bump,
  the prior major's published skill content is included as a *prior* (not
  a template).

### Output

A skill package directory:

- **`SKILL.md`** — agent-skills-spec-conformant. Frontmatter description
  is HyDE-rewritten to optimize retrieval (see below). Body is procedural
  prose: numbered steps, references to supporting files, and explicit tool
  calls.
- **`references/<group>.md`** per partition group touched. Reference content
  is **extracted** from Atlas entity docs, not freely generated. The
  synthesizer composes these from entity records, summarizes where docs
  are long, and adds a short "what you'll use this for" header. Extraction
  reduces hallucination risk dramatically.
- **`examples/*`** — working code/asset snippets. These are the highest-risk
  output. The discriminator's grounding pass is what catches example errors.
- **`scripts/*`** — optional deterministic helpers (e.g., a `.tscn`
  template generator). Generated only when the source adapter exposes
  patterns that benefit from helper scripts.
- **`.meta.json`** — sidecar (schema in [02](02-skill-packages.md)).

### Two-pass description optimization (HyDE for descriptions)

The frontmatter `description` is the only thing matched against during
retrieval. To bridge agent-vocab → API-vocab:

1. Generate the description in **API vocabulary** first ("Set up a
   RigidBody3D with a SphereShape3D collision shape...").
2. **Rewrite** in agent vocabulary ("Make a ball that rolls around with
   physics...").
3. **Embed both** as separate vectors stored under the same Qdrant point
   (Qdrant supports multiple named vectors per point). Agent queries match
   against either.

This hybrid description-vector is what closes the vocabulary gap discussed
in earlier planning.

### Section assembly

The synthesizer constructs the section list (the `.meta.json` `sections`
field) as it generates output:

1. Each Atlas group touched by the skill produces one `references/<group>.md`
   section, dependency = entities in that group.
2. Each example file is tagged with the group(s) whose entities it
   exercises.
3. SKILL.md procedure is split into steps; each step is annotated with
   the group it belongs to (using stable anchors per [02](02-skill-packages.md)).

This bookkeeping is what makes surgical updates possible (see [06](06-caching-and-updates.md)).

### Concurrency and budgets

- Per-snapshot synthesis is a batch job. Use cases are processed
  concurrently up to a configurable parallelism (default: 4 in-flight LLM
  calls).
- Each synthesis call has a token budget cap (default: 32k input, 8k
  output). Budget overrun triggers a smaller-model fallback or a "skip
  with diagnostic" outcome.
- The full ingest job is idempotent and resumable — every produced skill
  is checkpointed before the next is started.

## Discrimination

Decision 7: static discriminator only for v1; LLM-judge planned later.

The static discriminator is a deterministic pipeline. It runs after
synthesis and before publication. Failed checks block publication; the
skill is moved to `status = draft` with diagnostics in `.meta.json`.

### Pass 1 — Spec conformance

- `SKILL.md` parses; YAML frontmatter is valid.
- Required fields (`name`, `description`) present.
- `name` matches kebab-case pattern and is unique within `(source, source_major, skill_id)`.
- `description` length within bounds (configurable; default 50–600 chars).
- `allowed-tools`, if present, references known tool primitives.

### Pass 2 — Atlas grounding

For every API symbol, qualified name, or signature mentioned in the body
or examples:

- Resolve against the Atlas snapshot. Symbols that don't resolve are
  **hallucinations** — block.
- For method calls in examples, check arity and (if statically inferable)
  parameter types match the Atlas entity's signature.
- For class references, ensure inheritance chains mentioned (e.g.,
  "extends Node3D") are valid in the Atlas.

This is the highest-value check. It catches the dominant LLM failure mode
(invented APIs) cheaply.

Implementation: a lightweight parser per source kind extracts symbol
references from generated code/markdown. Source Adapters ship the parser
because symbol syntax is source-specific (GDScript vs. Python vs. TS).

### Pass 3 — Internal coherence

- Every file referenced from the body exists.
- Every entity in `.meta.json/atlas_dependencies` is referenced somewhere
  in the package (no dead deps).
- Every section's `entities` is a subset of `atlas_dependencies` (no
  dangling section refs).
- Section path anchors resolve in SKILL.md.

### Pass 4 — Retrieval fitness

The synthesized description is embedded and a small set of synthetic
queries (derived from the use case description and the Atlas partition
group names) is run against the live Qdrant index. The skill must
appear in the top-K for at least N of M synthetic queries (configurable;
defaults: K=5, N=3, M=5).

A skill that fails retrieval-fitness isn't *wrong* — it's just hard to
find. The discriminator returns it to synthesis with a request to
re-rewrite the description.

### Pass 5 (later, LLM-judge)

A future strict mode runs an LLM judge over the package contents asking
"would this skill, if followed correctly, accomplish the use case?"
Expensive; sampled rather than universal. Out of scope for v1 publication
gating but worth recording in `.meta.json` when run.

## Feedback loop

Decision 13: feedback aggregates at `(source, source_major, skill_id)`
and resets on major bump. Decision 15: cross-major feedback survives only
as soft prior, not as ranking signal.

### Capture

`mcp_semantic_gateway_submit_feedback` (see [04](04-planner.md)) writes
to `skill_feedback`. Each row carries the targeted skill version, optional
section path, signal, optional notes, and tenant.

### Aggregation

An offline job (or scheduled flush) rolls feedback up:

- Per skill version: positive count, negative count, note count.
- Per section within a skill: same counts. Surgical re-synthesis is
  triggered when negative count for a section exceeds threshold.
- Per skill (across versions in same major): rolling weighted score used
  as the `feedback_score` payload field in Qdrant for re-ranking.

### Trigger thresholds (defaults, tunable)

| Trigger | Threshold | Action |
|---|---|---|
| Negative feedback on a section | ≥3 within 7 days | Queue surgical re-synthesis of that section |
| Negative feedback on a skill (any section) | ≥10 within 14 days | Queue full skill re-synthesis |
| Note feedback | n/a | Concatenated into the next synthesis prompt as user signal |
| Positive feedback | n/a | Bumps `feedback_score` for ranking |

Re-synthesis runs through the same synthesis pipeline with the
accumulated feedback notes appended to the prompt as "known issues to fix."

### Cross-major prior

When a major bump triggers cold regeneration:

- The prior major's published skill content is included in the synthesis
  prompt as a *prior* ("here's how this was done in the previous version;
  use it as a hint").
- The prior major's `feedback_score` is **not** carried forward as a
  ranking signal in the new major.
- The prior major's negative-feedback **notes** ARE carried forward as
  "known issues to avoid in the new version."

This keeps majors honest (they earn their own rank) while preserving
hard-won negative signal.

## Open implementation choices

- **Section anchor stability.** As noted in [02](02-skill-packages.md),
  heading-slug anchors are fragile. UUID-comment anchors may be needed.
  Defer until empirically problematic.
- **Discriminator parallelism.** Passes 1–4 are independent; run them in
  parallel per skill. Trivial; flagged as implementation detail.
- **Feedback decay.** The 7-day / 14-day windows are placeholders.
  Real-world use will need decay calibration.
