# 07 — Knowledge MCP Surface

The knowledge surface is the smallest set of synthetic MCP tools that
expose the full knowledge layer — entities, patterns, use cases — to
any MCP client. It is the **v1 product gate**: when these tools work
end-to-end against a real source, the gateway has shipped its primary
value (decision N-6).

These tools live alongside the existing three (`mcp_semantic_gateway_context`,
`mcp_semantic_gateway_find_prompts`, `mcp_semantic_gateway_find_skills`)
and do not replace them.

## Tool roster

| Tool | Returns | Primary consumer |
|---|---|---|
| `mcp_semantic_gateway_query_api` | mixed bundle (entities + patterns + use cases + gaps) | direct agent use; planner |
| `mcp_semantic_gateway_describe_entity` | one entity + its edges + patterns it participates in | direct agent use; skill synthesis |
| `mcp_semantic_gateway_find_patterns` | patterns matching a query or seed entities | direct agent use; planner |
| `mcp_semantic_gateway_expand` | entity neighborhood at given depth | direct agent use |
| `mcp_semantic_gateway_local_usage` | references to a target entity from the user's local project | direct agent use; skill synthesis |

All four can be called without a planner and without skills. An agent
can take a question, query the knowledge layer, and reason from there.

## `mcp_semantic_gateway_query_api`

Primary entry point for agent-driven questions about an API.

**Input:**
```json
{
  "query": "How do I set up a sphere that rolls under gravity?",
  "options": {
    "sources": ["godot"],                  // optional; restrict source set
    "source_major": {"godot": 4},          // optional; pin major
    "snapshot_id": null,                   // optional; pin to specific snapshot
    "subjects": ["ingested", "local-project", "public-corpus"],   // optional; default = all enabled
    "include": ["entities", "patterns", "use_cases"],
    "top_k": 10,
    "tenant_id": null
  }
}
```

The `subjects` filter scopes pattern results to those whose evidence
draws from the named code-graph subjects. Useful for:
- "Show me only patterns we mined from Godot's own examples"
  (`["ingested"]`)
- "Show me only how my project uses this" (`["local-project"]`)
- "Show me what patterns the public ecosystem corroborates"
  (`["public-corpus"]`)

**Output:**
```json
{
  "query": "...",
  "results": {
    "entities": [
      {"entity_id": "godot.RigidBody3D", "qualified_name": "RigidBody3D", "kind": "class", "score": 0.92, "doc_excerpt": "..."}
    ],
    "patterns": [
      {
        "pattern_id": "pat-...",
        "kind": "idiom",
        "description": "Standard rolling-physics-body idiom: RigidBody3D + SphereShape3D + Vector3.UP impulse",
        "participants": ["godot.RigidBody3D", "godot.SphereShape3D", "godot.CollisionShape3D"],
        "evidence_count": 4,
        "evidence_subjects": {"ingested": 2, "public-corpus": 2},
        "confidence": 0.83,
        "determinism": "statistical",
        "score": 0.88
      }
    ],
    "use_cases": [
      {"use_case_id": "uc-...", "description": "Roll a ball under gravity...", "linked_patterns": ["pat-..."], "score": 0.95}
    ]
  },
  "supplementary_entities": [
    {"entity_id": "godot.PhysicsServer3D", "kind": "class", "via_edge": "references"}
  ],
  "gaps": []                               // populated if a use case matched without a linked pattern/skill
}
```

The agent reads the bundle, picks the right artifacts, and proceeds.
There is no obligatory "next call" — `query_api` is sufficient for
direct use.

## `mcp_semantic_gateway_describe_entity`

When the agent has an entity id (returned by `query_api`, or named
explicitly), this tool returns the full neighborhood.

**Input:**
```json
{
  "entity_id": "godot.RigidBody3D",
  "snapshot_id": null,                     // optional; default = latest published snapshot
  "include": ["edges", "patterns", "examples"]
}
```

**Output:**
```json
{
  "entity": {
    "id": "godot.RigidBody3D",
    "kind": "class",
    "qualified_name": "RigidBody3D",
    "signature": null,
    "doc": "...",
    "deprecated": false,
    "since_version": "4.0"
  },
  "edges": [
    {"kind": "extends", "target": "godot.PhysicsBody3D"},
    {"kind": "contains", "target": "godot.RigidBody3D.apply_impulse"},
    {"kind": "emits", "target": "godot.RigidBody3D.body_entered"}
  ],
  "patterns": [
    {"pattern_id": "pat-...", "kind": "idiom", "description": "...", "role": "subject"}
  ],
  "examples": [
    {"file_path": "doc/classes/RigidBody3D.xml", "excerpt": "..."}
  ]
}
```

This tool is what the skill-synthesis pipeline calls when assembling a
skill's `references/<class>.md` content (see [10](10-skill-generation.md)).

## `mcp_semantic_gateway_find_patterns`

Pattern-focused query. Two call modes:

**Mode A — by query:**
```json
{
  "query": "physics character with mouse-look camera",
  "kind": "idiom",                         // optional filter
  "determinism": "deterministic",          // optional: skip LLM-induced
  "top_k": 5
}
```

**Mode B — by seed entities:**
```json
{
  "seed_entities": ["godot.SpringArm3D", "godot.Camera3D"],
  "kind": null,                            // any kind
  "subjects": ["ingested", "local-project"],   // optional
  "top_k": 5
}
```

**Output:** list of `Pattern` records (same shape as in
`query_api.results.patterns`) with full body included.

Use case: an agent that knows two entities want to know "what idioms
combine these?"

## `mcp_semantic_gateway_local_usage`

Returns code-graph references from the user's local project (and
optionally public-corpus) to a target entity. Active when
`LocalCodeGraph` is enabled.

**Input:**
```json
{
  "entity_id": "godot.RigidBody3D",
  "subjects": ["local-project"],           // default; can include "public-corpus"
  "top_k": 20
}
```

**Output:**
```json
{
  "references": [
    {
      "subject": "local-project",
      "file_path": "scripts/player.gd",
      "line_start": 14,
      "line_end": 18,
      "context": "extends RigidBody3D\n\nfunc _ready():\n    apply_impulse(Vector3.UP * 5)",
      "confidence": 1.0
    }
  ]
}
```

This tool is what makes the local code graph directly useful: an agent
fielding "how do I use X here?" can pull the user's prior usage in
seconds.

## `mcp_semantic_gateway_expand`

Graph-walk over the Atlas + code graph.

**Input:**
```json
{
  "entity_ids": ["godot.RigidBody3D"],
  "depth": 2,
  "edge_kinds": ["extends", "contains", "emits"],
  "max_results": 25
}
```

**Output:** list of entities with the path that led to them.

Use case: building a structured prompt around a target entity (the
slicer in pattern mining uses the same primitive internally; agents
get it as a tool).

## Tool contracts and progressive disclosure

The MCP spec encourages **progressive disclosure**: tools should
return manageable sizes by default, with details available on follow-up
calls.

- `query_api` returns *summaries*, not full pattern bodies. The agent
  follows up with `find_patterns` (mode B with the pattern's
  participants) or `describe_entity` for details.
- `describe_entity` returns full doc, but `examples` are listed by
  reference; full example bytes come from a separate disclosure call
  (TBD as Phase-5 expansion).
- `expand` returns entities by id + qualified name, not bodies; agent
  pairs with `describe_entity` for any it cares about.

This keeps the typical first-call payload small enough to not blow
context budgets even on hub entities.

## Auth pass-through

Skills aren't the only thing that touches credentials. If the agent
follows up `query_api` results with an actual API call (e.g. against a
Stripe endpoint surfaced as an entity), the gateway's executor needs
the auth chain in place. The chain is identical to the prior design
([../api-introspection-skills/03-storage-and-retrieval.md#auth-pass-through](../api-introspection-skills/03-storage-and-retrieval.md#auth-pass-through))
and rides on the source's `AuthConfig`.

For the knowledge surface alone (which only reads ingested
descriptions, not the live API), no auth is required at query time —
ingestion's acquirer handled it.

## Multi-tenant filtering

Every tool accepts an optional `tenant_id`. Filters are applied as
payload predicates so a tenant sees only its own + global content.
Default tenant_id = `null` = global.

Tenant-specific overlay collections (a tenant's hand-curated patterns,
for instance) are a Phase-6+ extension; v1 ships with global content
only.

## Error surface

Each tool maps internal errors to one of:

- `INVALID_INPUT` — malformed query or filter.
- `UNKNOWN_SOURCE` — referenced source is not configured.
- `UNKNOWN_ENTITY` — `entity_id` doesn't resolve in the requested
  snapshot.
- `RATE_LIMITED` — for any LLM-touching path (HyDE, LLM rerank).
- `INDEX_UNAVAILABLE` — Qdrant connection issue.

Errors are returned as MCP tool errors per the protocol; the proxy
maps them to standardized JSON-RPC error codes.

## Observability

Every call records:

- `tool_name`, `tenant_id`, `source_set`, `latency_ms`.
- LLM token spend if HyDE / rerank fired.
- Top result ids (sampled) for retrieval-quality post-hoc review.

Surfaced via `mcp-semantic-gateway stats` CLI subcommand and an
optional structured log sink.

## What the surface does NOT do

- It does not synthesize skills. That's a separate consumer module.
- It does not decompose tasks. That's the planner.
- It does not modify the Atlas or run miners.
- It does not manage feedback. (Feedback tools live in skill-gen
  [10](10-skill-generation.md), since feedback is most useful against
  generated artifacts.)

## Open implementation choices

- **Streaming results.** `query_api` could stream patterns / use cases
  as they rerank. Defer until measured agent benefit.
- **Per-tool rate limits.** HyDE and reranker are LLM-touching; should
  rate-limit by tenant. v1 ships a single global limit; per-tenant is
  Phase-6 ergonomics.
- **A single `query` tool vs. four narrow tools.** Single-tool MCP
  surfaces are simpler to discover but harder to type. Four narrow
  tools are easier for the agent to call correctly. Sticking with
  four; revisit if telemetry shows agents underuse the narrow tools.
