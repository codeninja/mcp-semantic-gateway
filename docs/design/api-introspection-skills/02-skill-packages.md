# 02 — Skill Packages

Skills produced by this system conform to the **agent-skills specification**:
a directory containing a `SKILL.md` (YAML frontmatter + markdown body) and
optional supporting files for progressive disclosure. The agent reads the
frontmatter `description` to decide whether the skill is relevant; on use,
it loads the full directory.

We do not redefine the skill format. We add an adjacent `.meta.json` sidecar
that carries everything the spec doesn't, so SKILL.md stays clean and
portable.

## On-disk layout

Project-local data root is `.mcp_semantic_gateway/` (decision 18). Skills
live under it, partitioned by source and source version.

```
.mcp_semantic_gateway/
├── config.toml
├── atlas.db                          # SQLite: sources, snapshots, entities, edges
├── atlas/
│   └── godot/
│       └── 4.4.1/
│           ├── snapshot.json         # full atlas snapshot, content-addressed
│           └── raw/                  # cached raw source artifacts
└── skills/
    └── godot/
        └── 4.4.1/                    # source version
            ├── rolling-ball-physics/
            │   ├── v1/
            │   │   ├── SKILL.md
            │   │   ├── .meta.json
            │   │   ├── references/
            │   │   │   ├── rigid_body_3d.md
            │   │   │   └── physics_server_3d.md
            │   │   ├── examples/
            │   │   │   └── rolling_ball.gd
            │   │   └── scripts/      # optional, deterministic helpers
            │   └── v2/
            │       └── ...
            └── third-person-camera/
                └── v1/
                    └── ...
```

### Why `<source-version>/<skill-id>/v<n>/` and not `<skill-id>/<source-version>-v<n>/`

Two version axes — source-API version and skill version — and the source
version is the dominant one. Skills don't survive across major source
versions (decision 14), so the source version comes first in the path.
Within a source version, skill versions chain via `parent_version` in
`.meta.json` (lineage roots reset on major bump).

### Project-local vs global

Generated skills live in the project's `.mcp_semantic_gateway/`. The
existing `~/.mcp_semantic_gateway/` global directory continues to hold
user-level config and any globally-installed skills. The CLI resolves
project-local first; falls back to global. This matches the existing init
behavior.

## SKILL.md format

Per the agent-skills spec. Required frontmatter fields:

```yaml
---
name: rolling-ball-physics
description: |
  Build a rigid-body sphere that rolls under gravity, responds to impulses,
  and collides with static geometry. Covers RigidBody3D setup, collision
  shapes, and the _physics_process loop.
allowed-tools: []
---
```

- `name` — kebab-case, unique within `(source, source_major, skill_id)`
  scope. The skill_id is the directory name.
- `description` — the **only** field used for retrieval matching, so it
  must encode the use case in agent-vocabulary, not API-vocabulary. The
  synthesizer optimizes this with a HyDE-style rewrite step (see [05](05-synthesis-and-validation.md)).
- `allowed-tools` — auto-derived from the Atlas entities the skill
  references (decision 12). For a Godot skill that needs to write `.tscn`
  files and run scripts, this expands to whatever filesystem / shell
  tools the agent's runtime exposes for that purpose.

Body: free-form markdown describing the procedure and which tools to use,
per the spec. The body MAY reference supporting files via relative links
(`see [references/rigid_body_3d.md](references/rigid_body_3d.md)`); the
agent's runtime resolves these via progressive disclosure.

## `.meta.json` sidecar

This is where everything outside the spec lives. The agent never sees this
file; only the gateway and the synthesis pipeline read/write it.

```json
{
  "schema_version": 1,
  "skill_id": "rolling-ball-physics",
  "skill_version": 1,
  "lineage_root": 1,
  "parent_version": null,
  "source": {
    "id": "godot",
    "version": "4.4.1",
    "major": 4,
    "snapshot_id": "snap-7f3a..."
  },
  "status": "published",
  "generation": {
    "generator_version": "synthesis@0.3.0",
    "model": "claude-sonnet-4-6",
    "use_case_id": "uc-godot-physics-rolling-ball",
    "generated_at": "2026-05-01T18:22:00Z",
    "discriminator_passes": ["spec-conformance", "atlas-grounding", "internal-coherence", "retrieval-fitness"]
  },
  "atlas_dependencies": [
    {"entity_id": "godot.RigidBody3D",                  "hash": "sha256:abc..."},
    {"entity_id": "godot.RigidBody3D.apply_impulse",    "hash": "sha256:def..."},
    {"entity_id": "godot.CollisionShape3D",             "hash": "sha256:..."},
    {"entity_id": "godot.SphereShape3D",                "hash": "sha256:..."},
    {"entity_id": "godot.Node._physics_process",        "hash": "sha256:..."},
    {"entity_id": "godot.Vector3.UP",                   "hash": "sha256:..."}
  ],
  "sections": [
    {
      "path": "references/rigid_body_3d.md",
      "group_id": "class:RigidBody3D",
      "entities": ["godot.RigidBody3D", "godot.RigidBody3D.apply_impulse"]
    },
    {
      "path": "references/physics_server_3d.md",
      "group_id": "class:PhysicsServer3D",
      "entities": ["godot.PhysicsServer3D"]
    },
    {
      "path": "examples/rolling_ball.gd",
      "group_id": "class:RigidBody3D",
      "entities": ["godot.RigidBody3D.apply_impulse", "godot.Vector3.UP"]
    },
    {
      "path": "SKILL.md#step-3-physics-loop",
      "group_id": "class:Node",
      "entities": ["godot.Node._physics_process"]
    }
  ],
  "feedback": {
    "uses": 0,
    "positive": 0,
    "negative": 0,
    "notes": []
  }
}
```

### Field semantics

- **`skill_version`** — monotonic integer, scoped to `(source_major, skill_id)`. Resets to 1 on major source bumps.
- **`lineage_root`** — `skill_version` of the first ancestor in the same major. For a freshly-generated skill, equals `skill_version`. After a major bump, the regenerated skill's `lineage_root` equals its own `skill_version` (cold start).
- **`parent_version`** — pointer to the immediate prior version within the same major. `null` for lineage roots.
- **`source.snapshot_id`** — pins the skill to the Atlas snapshot it was generated against. Used for surgical update diffs.
- **`status`** — one of `draft | published | quarantined | superseded`. Only `published` skills are returned by retrieval.
- **`generation.discriminator_passes`** — record of which discriminator checks the skill passed. A future stricter discriminator can re-validate at any time.
- **`atlas_dependencies`** — flat list of every entity the skill depends on, with the entity's hash at generation time. The diff between this list and the current Atlas snapshot drives surgical updates.
- **`sections`** — partition map. Each section's `entities` is a subset of `atlas_dependencies`; the union of all section entity lists equals `atlas_dependencies`.
- **`feedback`** — running aggregate. Per-tenant feedback rows live in SQLite; this is the rolled-up view.

### Section path conventions

- File-level sections: `references/foo.md`, `examples/bar.gd`
- SKILL.md fragment sections: `SKILL.md#<heading-anchor>`, where the anchor is a kebab-case slug of the heading. The synthesizer emits stable anchors as part of generation.

A section in SKILL.md is bounded by its heading and the next heading of
equal or higher level. Surgical updates to SKILL.md sections rewrite only
the bounded fragment; surrounding sections are byte-preserved.

## Versioning rules

- **New skill** — `skill_version = 1`, `lineage_root = 1`, `parent_version = null`.
- **Surgical update within same major** — `skill_version = previous + 1`, `lineage_root = previous.lineage_root`, `parent_version = previous.skill_version`.
- **Cold regeneration after major bump** — `skill_version = 1`, `lineage_root = 1`, `parent_version = null`. The prior major's skills remain at their last published version under the prior `<source-version>/` directory; they are not deleted.
- **Quarantine** — `status` flips to `quarantined`; `skill_version` does NOT change. Unquarantine produces a new version.
- **Supersession** — when version N+1 is published, version N's `status` flips to `superseded` but the directory is retained for rollback.

## Rollback

`mcp_semantic_gateway_rollback(skill_id, target_version)` (see [04](04-planner.md)):

1. Verify `target_version` exists and is not `quarantined` for an unresolved breaking-change reason.
2. Mark all later versions in the same major as `superseded` (not deleted).
3. Mark `target_version` as `published`.
4. Re-index — Qdrant points at the new published version's description.

## `allowed-tools` derivation

The synthesizer collects the set of execution-relevant capabilities the skill
requires by walking its `atlas_dependencies` and the Source Adapter's
declared tool mappings. Each adapter publishes a static map of "to use this
entity, you need these tool primitives":

```python
class SourceAdapter:
    tool_requirements: dict[EntityKind, list[str]]
    # e.g. for godot-xml:
    # {
    #     EntityKind.method: ["filesystem.write", "shell.run"],
    #     EntityKind.endpoint: [],   # n/a for this source
    # }
```

For OpenAPI sources, the requirements include the HTTP-call primitive plus
auth references resolved from MCP server config (see [03](03-storage-and-retrieval.md)).

The user can override the derived `allowed-tools` set by hand-editing
`SKILL.md`; the synthesizer respects an `allowed-tools` value already
present in a parent version when producing a surgical update, unless the
referenced entities have changed in ways that require new tools.

## Why a `.meta.json` sidecar instead of frontmatter extension

The agent-skills spec defines the frontmatter schema; adding fields would
break portability and risk colliding with future spec changes. A sidecar
keeps SKILL.md spec-pure, lets us evolve metadata independently, and is
naturally ignored by agents that only consume the skill content.

## Open implementation choices

- **SKILL.md fragment anchoring stability.** Heading-slug anchors are
  fragile if the synthesizer rewrites headings. We may need to assign each
  section a stable UUID embedded as an HTML comment (`<!-- section: 7f3a -->`)
  so surgical updates can target sections even when headings change. Defer
  until we see real instability in practice.
- **References to other skills.** A skill may want to recommend "see also:
  third-person-camera". Cross-skill references are not modeled in v1; they
  can be added as a `related_skills` field in `.meta.json` later.
