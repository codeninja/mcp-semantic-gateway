# 10 — Skill Generation

The skill-generation module is a *consumer* of the knowledge layer
(decision N-7). It produces agent-skills-spec-conformant skill packages
by reading from the Atlas, the code graph (for slices), and the pattern
store (for high-confidence implementation idioms). It exposes its own
retrieval surface (`find_skills`) and feedback hooks.

This module is optional: a deployment can run the knowledge gateway and
the planner without ever installing skill-gen. When it *is* installed,
it ships in Phase 7 — well after the knowledge surface is in
production use.

The bulk of this module's design is inherited from the prior plan
([../api-introspection-skills/02-skill-packages.md](../api-introspection-skills/02-skill-packages.md),
[../api-introspection-skills/05-synthesis-and-validation.md](../api-introspection-skills/05-synthesis-and-validation.md)).
This document records the deltas.

## What carries forward unchanged

- agent-skills spec conformance for skill packages.
- `.meta.json` sidecar carrying provenance, atlas dependencies,
  sections, lineage, feedback.
- On-disk layout: `.mcp_semantic_gateway/skills/<source>/<source-version>/<skill-id>/v<n>/`.
- `allowed-tools` derived automatically from Atlas entities.
- Static discriminator with four passes (spec, grounding, coherence,
  retrieval-fitness).
- Feedback aggregation at `(source, source_major, skill_id)`; resets on
  major bump.
- HyDE-style two-vector frontmatter description.
- Section partitioning along Parser-declared partition hints.

## Deltas

### Synthesis input includes patterns

Where the prior design seeded skill synthesis from a use case + Atlas
region, this design seeds from a use case + relevant **patterns** + the
code graph slice for the central entities.

```python
class SkillSynthesisInput(BaseModel):
    use_case: UseCase
    central_entities: list[str]
    related_patterns: list[Pattern]            # NEW: high-confidence patterns covering the entities
    code_slices: list[Slice]                   # NEW: code-graph slices for central entities
    partition_hints: PartitionMap
    carry_forward_priors: list[SkillContent]   # prior major's content if regenerating
```

Patterns dramatically reduce hallucination by giving the synthesizer
real, validated structural exemplars instead of asking it to invent
from entity docs alone.

### Discriminator reuses pattern grounding

The discriminator's grounding pass already verifies that every API
symbol in generated content resolves in the Atlas. New addition:
**pattern attribution**. Skills that distill from a specific idiom
record the pattern id in `.meta.json`, and the grounding pass verifies
the pattern still exists (not quarantined / superseded).

```json
{
  ...,
  "atlas_dependencies": [...],
  "pattern_dependencies": [                    // NEW
    {"pattern_id": "pat-...", "hash": "..."}
  ],
  ...
}
```

When a pattern is invalidated, downstream skills referencing it are
flagged for review by the same surgical-update pipeline that handles
entity changes.

### `find_skills` Protocol replaces direct collection access

Skill-gen owns its Qdrant collection (`skills`) and exposes a
`SkillRetrieval` Protocol to consumers (the planner). The
knowledge-index module does not import skill-gen.

```python
class SkillRetrieval(Protocol):
    async def find_skills(
        self,
        query: str,
        *,
        filters: Filters,
        top_k: int = 10,
    ) -> list[SkillHit]: ...
```

When the planner is configured with skill-gen present, it composes
`KnowledgeIndex.find_*` and `SkillRetrieval.find_skills` results in a
single fusion step. When skill-gen is absent, the planner skips the
skill call entirely.

### Skill MCP tools

Three tools, registered only when skill-gen is online:

- `mcp_semantic_gateway_get_skill(skill_id, version?, source?)` —
  returns SKILL.md + manifest of supporting files.
- `mcp_semantic_gateway_submit_feedback(skill_id, version, signal, options?)` —
  writes feedback row.
- `mcp_semantic_gateway_rollback(skill_id, target_version, source?)` —
  reverts to prior version.

These match the prior design's contracts exactly.

The planner's `mcp_semantic_gateway_plan` tool registers regardless of
skill-gen, but the `skill_binding` field of `SubGoal` is null when
skill-gen is absent.

## Synthesis pipeline

```
For each (use_case_id) to synthesize:
  1. Load central entities, related patterns, code slices.
  2. Compose synthesis prompt:
     - System: agent-skills spec contract + house rules.
     - Use case description.
     - Atlas region (entity catalog with signatures + docs).
     - Related patterns (description + body + evidence).
     - Code slices (deduplicated examples + caller/callee context).
     - Carry-forward priors if regenerating.
  3. LLM call (configurable model). Structured output: SKILL.md +
     references + examples + section map.
  4. Two-vector description rewrite (API-vocab + agent-vocab).
  5. Run discriminator (4 passes in parallel):
     - Spec conformance
     - Atlas grounding
     - Pattern attribution (NEW)
     - Coherence
     - Retrieval fitness
  6. If all pass: write package, update SQLite, embed both vectors,
     publish to Qdrant.
  7. If any fails: store as draft with diagnostics; do not publish.
```

Concurrency and budgets follow the prior design.

## Surgical updates for skills

When a snapshot diff produces changed entities or invalidated
patterns, the skill-gen update pipeline runs alongside the
pattern-mining update pipeline.

```
For each published skill:
  affected_deps = atlas_deps ∩ diff.changed_entities
  affected_patterns = pattern_deps ∩ diff.invalidated_patterns

  if both empty: fast-forward
  elif all affected are non-breaking: surgical re-synthesis of dirty sections
  else: quarantine
```

The prior design's section-level surgical update logic is preserved.

## Feedback loop

Identical to the prior design. The only addition is that feedback
notes can now reference patterns:

```json
{
  "skill_id": "rolling-ball-physics",
  "skill_version": 2,
  "signal": "negative",
  "section_path": "examples/rolling_ball.gd",
  "notes": "the apply_impulse pattern this distills from is using stale syntax",
  "pattern_reference": "pat-..."             // NEW: optional
}
```

When negative feedback names a pattern, the pattern miner is also
queued to re-evaluate that pattern. This closes the loop between
agent feedback and the underlying knowledge.

## Configuration

```toml
[skill-gen]
enabled = true                                # off by default in v1; opt-in
synthesis_model = "claude-sonnet-4-6"
synthesis_concurrency = 4
synthesis_budget_tokens_in = 32000
synthesis_budget_tokens_out = 8000
discriminator_strict = true
```

Disabled skill-gen means: no skill MCP tools registered, no `skills`
Qdrant collection, no synthesis pipeline runs. The knowledge gateway
operates fully without it.

## What skill-gen does NOT do

- It does not own the Atlas or the code graph (read-only consumers).
- It does not run pattern miners (consumes patterns as input).
- It does not decompose tasks (the planner does).
- It does not change the knowledge surface contracts.

## Open implementation choices

- **Pattern-attribution boundaries.** When does a skill "distill from"
  a pattern vs. just happen to use the same entities? Heuristic for v1:
  if the skill's central-entity set ⊇ the pattern's participants, the
  pattern is recorded as a dependency. Refine with telemetry.
- **Skill-pattern bidirectional consistency.** A skill encoding
  pattern X should not contradict X's `body` (e.g., wrong sequencing).
  v1 catches this only via Atlas grounding (checks symbols, not
  sequencing). Sequencing checks are a Phase-8+ extension.
- **Skill cross-source bundling.** A task spanning two sources
  produces two skill packages today (one per source); we may want a
  combined "scenario" skill that orchestrates both. Out of scope for
  v1.
