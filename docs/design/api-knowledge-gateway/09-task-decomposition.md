# 09 — Task Decomposition

The task-decomposition module — the **planner** — is a *consumer* of
the knowledge layer (decision N-7). It takes an agent's natural task
and produces a structured decomposition where each sub-goal is bound
to one or more knowledge artifacts: entities, patterns, use cases, or
(when skill-gen is online) skills.

The planner does not own retrieval. It calls the knowledge surface and
the (optional) skill surface and composes their results. It runs as a
separate module so that:

- A deployment can ship the knowledge gateway without a planner.
- Tests can mock the knowledge surface and exercise planner logic in
  isolation.
- The knowledge surface evolves without breaking the planner contract.

## Planner Protocol

```python
class Planner(Protocol):
    async def plan(self, task: str, options: PlanOptions) -> Plan: ...

class PlanOptions(BaseModel):
    sources: set[str] | None = None         # restrict to configured sources
    source_pinning: dict[str, int] | None = None
    snapshot_pinning: dict[str, str] | None = None
    tenant_id: str | None = None
    max_steps: int = 20
    bind_score_threshold: float = 0.75
    bind_margin_threshold: float = 0.10
    include_skills: bool = True              # ignored if skill-gen not online
    synthesis_on_gap: bool = False           # Phase-7+ flag
```

```python
class Plan(BaseModel):
    plan_id: str
    task: str
    decomposition: list[SubGoal]
    warnings: list[str]
```

```python
class SubGoal(BaseModel):
    step: int
    sub_goal: str
    knowledge_bindings: list[KnowledgeBinding]
    skill_binding: SkillBinding | None = None    # populated if a skill matches confidently
    gap: Gap | None = None                       # populated when nothing binds confidently
    supplementary_entities: list[EntityHit] = []

class KnowledgeBinding(BaseModel):
    kind: Literal["entity", "pattern", "use-case"]
    artifact_id: str
    score: float
    why: str                                     # short rationale: "matches sub-goal vocabulary"

class SkillBinding(BaseModel):
    skill_id: str
    skill_version: int
    source: str
    package_path: str
    score: float

class Gap(BaseModel):
    reason: Literal["no-confident-match", "no-skill-match", "no-pattern-match"]
    nearest_use_case_id: str | None = None
    candidate_entities: list[str] = []
```

## Behavior

```
1. Decompose the task into sub-goals via an LLM call.
   Input: task + compact catalog of available sources (partition group ids only).
   Output: structured sub-goal list (3–10 typical).

2. For each sub-goal in parallel:
   a. Call knowledge_index.find_use_cases(sub_goal) → top use cases
   b. Call knowledge_index.find_patterns(sub_goal)  → top patterns
   c. Call knowledge_index.find_entities(sub_goal)  → top entities
   d. If skill-gen is online: call skill_retrieval.find_skills(sub_goal)
   e. Merge + RRF rerank → ranked candidates
   f. Bind:
      - If a skill matches above threshold + margin → SkillBinding
      - Else, take top non-skill candidates as KnowledgeBindings
      - If no candidate above threshold → emit Gap

3. Run graph expansion on bound entities/patterns to populate
   supplementary_entities.

4. Cache plan keyed by hash(task + filter_context). TTL 5 min.
```

## Why bind to knowledge artifacts, not just skills

The prior design treated skills as the only binding target; gaps were
emitted whenever no skill matched. This was too coarse — it let
useful entity-level and pattern-level information go unused.

In this design:

- A sub-goal that has no matching skill but a perfect-match idiom
  pattern + 3 entity hits is **not a gap** — it's a knowledge binding
  the agent can use directly to write code.
- A sub-goal that matches a use case but has no linked pattern *or*
  skill *is* a gap — known intent, no implementation guidance.
- A sub-goal with no matches at any tier is a deeper gap — neither
  intent nor implementation is recognized.

Granular gap classification is what makes the planner useful even
when skill-gen is off entirely.

## Decomposer prompting

The decomposer LLM gets:

1. **The task.**
2. **A compact catalog of available sources** — for each source, a
   one-line description and the list of partition group ids (e.g., for
   Godot, the list of class names; for OpenAPI, the list of tags).
   Small enough to fit in context even for large APIs because we list
   groups, not individual entities.
3. **A schema for structured output** — sub-goal list shape.
4. **Decomposition heuristics** — prefer 3–10 steps; sub-goals
   expressible in API vocabulary; group related capabilities into
   single sub-goals.

Output is structured (JSON via tool-use). The decomposer never emits
bindings itself; that's the retrieval-and-bind step's job.

## Confidence thresholds

Defaults:

- Bind a skill if reranked score > 0.75 AND margin > second match > 0.10.
- Bind a pattern if reranked score > 0.70 AND it has a non-LLM-induced
  source OR confidence > 0.85.
- Bind a use case if its `linked_patterns` is non-empty (i.e. it has
  implementation guidance).
- Else emit a Gap, with `nearest_use_case_id` if any use case matched
  the sub-goal regardless of links.

These numbers are placeholders; calibrate during Phase 6 against real
queries.

## Plan caching

Cache key: `hash(task + sorted(source_pinning) + sorted(snapshot_pinning) + tenant_id)`.

TTL: 5 minutes. Cache also invalidates when:

- A new snapshot is ingested for any pinned source.
- A skill is published, quarantined, or rolled back.
- A pattern moves between published / quarantined / superseded.

Per-tenant by default (tenant-specific pinnings differ).

## CLI surface

```
mcp-semantic-gateway plan "<task>" [--source godot] [--major godot=4]
mcp-semantic-gateway plan-debug "<task>"      # verbose, with retrieval traces
```

## What the planner does NOT do

- **Execute.** The agent runs the procedure; we don't.
- **Verify outcomes.** The agent verifies its own actions.
- **Synthesize at runtime.** `synthesis_on_gap` is a Phase-7+ flag and
  bounded by a budget when enabled.
- **Choose between competing skills.** Returns the top skill match
  and lets the agent decide; doesn't pick.

## Open implementation choices

- **Decomposer model selection.** Sonnet-class for v1 quality;
  Haiku-class as cheaper option. Per-deployment config.
- **Streaming plans.** Stream sub-goals as resolved; defer to Phase-7+
  for the contract complexity.
- **Cross-source plans.** A task that spans sources (e.g., "fetch from
  Stripe and store in Postgres") yields sub-goals from different
  sources. v1 supports this trivially — bindings are per-source —
  but cross-source dependency tracking is post-v1.
