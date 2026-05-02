# 04 — Planner and MCP Surface

The planner is the new gateway-side component that decomposes an agent task
into sub-goals and binds each sub-goal to a skill — or returns a *gap* when
no skill matches. It sits above the retrieval layer and is exposed to
agents via a synthetic MCP tool.

Decision 9 places the planner in this codebase rather than leaving
decomposition to the calling agent. The reason is leverage: with a planner,
the gateway can apply structured retrieval (per-sub-goal queries, graph
expansion, gap detection) that an agent calling raw `_find_skills` would
not. Without a planner, the retrieval surface is too primitive for the
agent to use well.

## Synthetic MCP tools

The current proxy injects three synthetic tools (`mcp_semantic_gateway_context`,
`mcp_semantic_gateway_find_prompts`, `mcp_semantic_gateway_find_skills`) at
`integration/proxy.py:40`. We add four more.

### `mcp_semantic_gateway_plan(task, options?)`

Primary entry point for the new feature.

**Input:**
```json
{
  "task": "Build a third-person camera with collision avoidance for a 3D platformer",
  "options": {
    "source_pinning": {"godot": 4},          // optional; pin to source major
    "tenant_id": "alice",                    // optional
    "max_steps": 20,                         // optional cap
    "synthesis_on_gap": false                // Phase-6 flag; default false
  }
}
```

**Output:**
```json
{
  "plan_id": "plan-7f3a...",
  "task": "...",
  "decomposition": [
    {
      "step": 1,
      "sub_goal": "Set up a SpringArm3D-attached Camera3D following the player",
      "skill_binding": {
        "skill_id": "third-person-camera",
        "skill_version": 2,
        "source": "godot",
        "source_version": "4.4.1",
        "package_path": ".mcp_semantic_gateway/skills/godot/4.4.1/third-person-camera/v2"
      },
      "supplementary_entities": [
        {"entity_id": "godot.SpringArm3D", "kind": "class"},
        {"entity_id": "godot.Camera3D", "kind": "class"}
      ]
    },
    {
      "step": 2,
      "sub_goal": "Configure the spring arm to absorb collisions with level geometry",
      "skill_binding": null,
      "gap": {
        "reason": "no-skill-match",
        "nearest_use_case_id": "uc-godot-camera-collision-tuning",
        "candidate_entities": ["godot.SpringArm3D.spring_length", "godot.SpringArm3D.add_excluded_object"]
      }
    },
    {
      "step": 3,
      "sub_goal": "Wire input axes to camera rotation",
      "skill_binding": {
        "skill_id": "input-action-mapping",
        "skill_version": 1,
        "source": "godot",
        "source_version": "4.4.1",
        "package_path": ".mcp_semantic_gateway/skills/godot/4.4.1/input-action-mapping/v1"
      }
    }
  ],
  "warnings": []
}
```

**Behavior:**

1. LLM-driven decomposition: the planner calls a structured-output LLM with
   the task plus a brief catalog of available source(s). Output is a tree
   of sub-goals.
2. For each sub-goal, retrieval runs (see [03](03-storage-and-retrieval.md))
   against the `skills` and `use_cases` collections in parallel.
3. Binding logic:
   - If a skill matches above the configured confidence threshold → bind it.
   - If a use case matches but has no linked skill → emit a `gap` with the
     use case as the nearest reference.
   - If neither matches → fall through to entity-level retrieval and emit
     a `gap` with `candidate_entities`.
4. Graph expansion runs on each bound skill to populate `supplementary_entities`.
5. If `synthesis_on_gap` is true and the Phase-6 capability is enabled,
   each gap triggers a runtime synthesis attempt (out of scope for v1).

The agent reads the plan, picks an order (the decomposition is a *suggested*
order, not mandatory), and executes by loading skill packages and following
their procedures. The agent verifies its own results.

### `mcp_semantic_gateway_get_skill(skill_id, version?, source?)`

Returns a skill package's contents for direct loading.

**Input:**
```json
{
  "skill_id": "rolling-ball-physics",
  "version": 2,                              // optional; defaults to latest published
  "source": {"id": "godot", "major": 4}      // optional disambiguation if skill_id collides across sources
}
```

**Output:** the SKILL.md content plus a manifest of supporting files. The
agent's runtime loads the supporting files via progressive disclosure on
demand.

### `mcp_semantic_gateway_submit_feedback(skill_id, version, signal, options?)`

Closes the feedback loop.

**Input:**
```json
{
  "skill_id": "rolling-ball-physics",
  "skill_version": 2,
  "signal": "negative",                      // 'positive' | 'negative' | 'note'
  "section_path": "examples/rolling_ball.gd",  // optional; targets a specific section
  "notes": "apply_impulse expects a Vector3, not a tuple — example uses tuple syntax",
  "tenant_id": "alice"                       // optional
}
```

**Output:** `{ "accepted": true, "feedback_id": "fb-..." }`

Feedback writes a row into `skill_feedback`. An offline aggregator updates
`feedback_score` on the skill row periodically (not synchronously, to keep
write latency low). When negative feedback for a section accumulates above
threshold, the skill is queued for surgical re-synthesis (see [05](05-synthesis-and-validation.md)).

### `mcp_semantic_gateway_rollback(skill_id, target_version, source?)`

Restricted to operators / authorized tenants. Reverts a skill to a prior
version. Versioning rules in [02](02-skill-packages.md).

**Input:**
```json
{
  "skill_id": "rolling-ball-physics",
  "target_version": 1,
  "source": {"id": "godot", "major": 4}
}
```

**Output:** `{ "rolled_back_to": 1, "previous_published": 3 }`

## How the planner interacts with retrieval

The planner does NOT embed retrieval logic. It calls into a `RetrievalService`
that abstracts the Qdrant + SQLite operations. This separation matters
because:

- The retrieval service is also called by `_find_skills` directly (existing
  surface).
- Tests can mock retrieval and exercise planner logic in isolation.
- Future runtime synthesis (Phase 6) plugs into the service interface
  without planner changes.

```python
class RetrievalService(Protocol):
    async def find_skills(self, query: str, *, filters: RetrievalFilters, top_k: int = 10) -> list[SkillMatch]: ...
    async def find_use_cases(self, query: str, *, filters: RetrievalFilters, top_k: int = 10) -> list[UseCaseMatch]: ...
    async def find_entities(self, query: str, *, filters: RetrievalFilters, top_k: int = 10) -> list[EntityMatch]: ...
    async def expand_graph(self, entity_ids: list[str], *, depth: int = 1) -> list[EntityMatch]: ...
```

## Decomposition prompting

The decomposer LLM is prompted with:

1. **The task.**
2. **A compact catalog of available sources** — for each configured source,
   a short description and a list of partition group ids (e.g., for Godot,
   the list of class names). This is small enough to fit in the prompt
   even for large APIs, because we list groups, not individual entities.
3. **A schema for structured output** — the sub-goal tree shape.
4. **Decomposition heuristics** — a short rubric: prefer 3–10 steps,
   avoid sub-goals that can't be expressed in API vocabulary, group
   related capabilities into single sub-goals.

Output is structured (JSON via tool-use). The decomposer never emits skill
bindings itself — that's the retrieval step's job.

## Confidence threshold and gap policy

The threshold for "bind a skill" vs "emit a gap" is configurable per source.
Defaults:

- Bind if the top skill match's reranked score exceeds 0.75 AND the score
  margin to the second match is at least 0.10.
- Otherwise, emit a gap referencing the nearest use case (if any) and the
  top-K Atlas entities the agent can improvise from.

These numbers are placeholders; they need calibration against real query
distributions and will be tuned during Phase 3.

## Caching

Plan caching is key-by-task plus filter context:
`cache_key = hash(task_text + sorted(source_pinning) + tenant_id)`.

TTL: 5 minutes by default (a plan is a snapshot of "what skills exist
right now"). Cache invalidation also fires when a source ingest produces
a new snapshot or a skill is published / quarantined.

## What the planner deliberately does NOT do

- **Execute skills.** The agent runs the skill content; we don't.
- **Verify outcomes.** The agent checks whether its action had the
  intended effect.
- **Make tools/list explode.** Skills are returned as plan content, not
  registered as individual MCP tools.
- **Block on synthesis.** Even with `synthesis_on_gap = true`, the call is
  bounded by a synthesis budget; it returns a gap rather than hanging.

## Open implementation choices

- **Decomposer model.** Sonnet-class is appropriate for v1 quality;
  Haiku-class may be sufficient for cheaper deployments. Per-deployment
  config.
- **Plan caching scope.** Per-tenant or global? Per-tenant is safer
  (tenant-specific source pinning); global is cheaper. v1: per-tenant.
- **Streaming plans.** The planner could stream sub-goals as they're
  resolved. Useful for large plans; complicates the MCP tool contract.
  Defer to Phase 4+.
