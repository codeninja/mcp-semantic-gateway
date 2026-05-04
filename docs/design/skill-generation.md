# Skill Generation — Design

This document specifies the **skill synthesis stage** of the ingest
pipeline: how persisted use case records (produced upstream by
[use-case-synthesis.md](use-case-synthesis.md)) are clustered and
turned into agent-skills-specification-conformant skill packages cached
in the project folder.

This is a pragmatic lift of
[api-introspection-skills/02-skill-packages.md](api-introspection-skills/02-skill-packages.md)
and
[api-knowledge-gateway/10-skill-generation.md](api-knowledge-gateway/10-skill-generation.md),
reduced to what is shippable on top of the current code without an
Atlas, code graph, or pattern store. The discriminator is preserved in
spirit (cheap deterministic grounding) but tightly scoped to
"hallucinated tool names cannot ship."

## Goal

For each opted-in source, take the use cases mined upstream, group
related ones into a single skill (one skill may cover multiple use
cases), and synthesize an agent-skills-spec-conformant package. Cache
the package in the project folder under
`.mcp_semantic_gateway/skills/<server_id>/<source_hash[:12]>/<skill-id>/v1/`
so the existing `Collector.collect_skills()` path discovers it on the
next index pass with no retrieval-side changes.

## Scope

In scope:

- Use-case clustering by semantic similarity.
- LLM-driven skill synthesis, one call per cluster.
- agent-skills-spec-conformant `SKILL.md` (frontmatter `name`,
  `description`, `allowed-tools`) plus a `.meta.json` sidecar.
- Cheap deterministic validation (no second LLM call): frontmatter parses,
  every backticked tool name resolves to a harvested tool, description
  length within bounds.
- Project-local cache layout, idempotent on `source_hash`.
- Observability shared with use-case mining (same `EventEmitter`,
  additional skill-stage events).

Out of scope (deliberately deferred):

- HyDE two-vector descriptions.
- Pattern-attribution (`pattern_dependencies` in `.meta.json`).
- Section-level surgical updates.
- Quarantine / supersession state machines.
- Feedback aggregation, rollback, cross-major regeneration.
- LLM-judge discriminator pass.

## Decisions log

| # | Decision |
|---|---|
| S-1 | Skill bodies conform to the agent-skills specification: YAML frontmatter (`name`, `description`, optional `allowed-tools`) + procedural markdown body. No deviations from the spec. |
| S-2 | Project-local cache root: `.mcp_semantic_gateway/skills/<server_id>/<source_hash[:12]>/<skill-id>/v1/`. Skills do NOT live under the global `~/.mcp_semantic_gateway/`. |
| S-3 | A skill is generated per **use-case cluster**, not per use case. One skill may cover multiple related use cases. |
| S-4 | Clustering: cosine similarity on use-case description embeddings (existing `LocalEmbedder`); threshold default 0.78. Singleton clusters are valid. |
| S-5 | Synthesis is one LLM call per cluster, structured output via tool-use / function-calling on the abstraction defined in [use-case-synthesis.md](use-case-synthesis.md). |
| S-6 | `allowed-tools` is auto-derived from harvested tool names referenced in the body. Hand-edits are preserved on subsequent re-runs (cache hit on cluster_hash). |
| S-7 | Validation is deterministic only (no second LLM call). Hallucinated tool name = drop skill, write diagnostic. |
| S-8 | Generated skills are picked up by the existing `SourceType.SKILL` collector path; no changes to retrieval or the embedder. |
| S-9 | Idempotent cache key: `(server_id, source_hash, cluster_hash, model_id, prompt_version)`. Re-running the pipeline on unchanged inputs = zero LLM calls. |

## Module layout

New files under `src/mcp_semantic_gateway/`:

```
ingestion/
├── skill_clusterer.py               # cluster_use_cases(records) -> list[UseCaseCluster]
├── skill_synthesizer.py              # synthesize_skill(cluster, harvested_tools, llm) -> SkillPackage
├── skill_validator.py                # validate_skill(package, harvested_tools) -> ValidationReport
└── skill_writer.py                   # write_package(package, project_root) -> Path
```

Reuses (no new deps):

- `llm/` abstraction from [use-case-synthesis.md](use-case-synthesis.md).
- `ingestion/observability.py` `EventEmitter` (additional skill stages).
- `ingestion/embedder.py` `LocalEmbedder` for cluster similarity.
- `Collector.collect_skills()` for the discovery path on next index run.

## Inputs

```python
# skill_clusterer.py
class UseCaseCluster(BaseModel):
    cluster_id: str                       # 'cluster-<server>-<seq>'
    server_id: str
    source_hash: str
    use_case_ids: list[str]               # references into use_cases table
    tool_name_union: list[str]            # union of linked_tool_names across members
    centroid_description: str             # representative description (medoid)
    cluster_hash: str                     # sha256 of sorted (use_case_hash...) members
```

Clustering algorithm (deterministic, no LLM):

1. Load use-case records for `(server_id, source_hash)`.
2. Embed descriptions with `LocalEmbedder`.
3. Greedy agglomerative clustering: for each unassigned record, attach
   to the highest-similarity existing cluster if cosine ≥ threshold;
   otherwise open a new cluster.
4. Compute medoid (closest member to cluster centroid) → `centroid_description`.
5. Compute `cluster_hash` from sorted member `use_case_hash` values.

Threshold default 0.78 (decision S-4); tunable in
`[skill_generation] cluster_threshold`.

## Skill package shape

### On-disk layout

```
.mcp_semantic_gateway/
└── skills/
    └── <server_id>/
        └── <source_hash[:12]>/
            └── <skill-id>/
                └── v1/
                    ├── SKILL.md
                    ├── .meta.json
                    └── references/
                        └── <group_label>.md
```

`<skill-id>` is a kebab-case slug derived from the synthesizer's chosen
skill name; collisions within a `(server_id, source_hash)` are
disambiguated with a numeric suffix.

### `SKILL.md` (agent-skills spec)

```markdown
---
name: github-issue-triage
description: |
  Search, label, comment, and close GitHub issues based on triage rules.
  Combines repo issue search with bulk label and comment operations.
allowed-tools:
  - github_search_issues
  - github_add_labels
  - github_create_comment
  - github_close_issue
---

## When to use this skill

...

## Procedure

1. Identify candidate issues using `github_search_issues`. ...
2. Apply labels with `github_add_labels`. ...
3. Post a triage comment with `github_create_comment`. ...
4. Close stale issues with `github_close_issue` when ...

## Notes
...
```

The body is procedural prose with explicit references to harvested
tool names (the tools the gateway already exposes). The synthesizer
must reference tool names verbatim from the harvested set; the
validator drops any skill that mentions a non-existent name.

### `.meta.json` sidecar

```json
{
  "schema_version": 1,
  "skill_id": "github-issue-triage",
  "skill_version": 1,
  "source": {
    "server_id": "github-api",
    "source_hash": "sha256:...",
    "source_hash_short": "ab12cd34..."
  },
  "cluster": {
    "cluster_id": "cluster-github-api-3",
    "cluster_hash": "sha256:...",
    "use_case_ids": ["uc-github-api-issues-0-1", "uc-github-api-issues-2-0"]
  },
  "tool_dependencies": [
    "github_search_issues",
    "github_add_labels",
    "github_create_comment",
    "github_close_issue"
  ],
  "generation": {
    "model": "claude-sonnet-4-6",
    "prompt_version": "v1",
    "generated_at": "2026-05-04T10:24:11Z",
    "validation_passes": ["spec-conformance", "tool-grounding", "length-bounds"]
  },
  "status": "published"
}
```

The agent never reads `.meta.json`; only the gateway and synthesis
pipeline do. This keeps `SKILL.md` portable and spec-pure.

## Synthesis pipeline

```
synthesize_for_source(server_id, source_hash):
  1. Load use cases for (server_id, source_hash).
  2. clusters = cluster_use_cases(use_cases)
  3. emit 'clustering_completed' (cluster_count, singleton_count)
  4. async with semaphore(max_concurrency):
       for cluster in clusters:
         emit 'skill_synthesis_started' (cluster_id)
         if cache_hit(server_id, source_hash, cluster_hash, model_id, prompt_version):
           emit 'skill_cache_hit', skip
           continue
         result = await llm.call(
           messages = [system_prompt, user_prompt(cluster, harvested_tools)],
           tools = [SKILL_EMISSION_TOOL_SCHEMA],
           force_tool = 'emit_skill_package',
         )
         package = build_package_from_tool_call(result, cluster, server_id, source_hash)
         report = validate_skill(package, harvested_tools)
         if report.passed:
           write_package(package, project_root)
           emit 'skill_written' (skill_id, file_path, tool_count)
         else:
           write_diagnostic(package, report)
           emit 'skill_rejected' (skill_id, reasons)
  5. emit 'synthesis_completed' (skills_written, skills_rejected, total_usage)
```

### Tool-use schema for synthesis

The synthesizer registers one tool whose input schema captures every
field needed to construct the package. The model's tool call arguments
serialize directly into a `SkillPackage` dataclass. No prose parsing.

```python
SKILL_EMISSION_TOOL_SCHEMA = ToolSpec(
    name="emit_skill_package",
    description="Emit one agent-skills-spec-conformant skill package.",
    input_schema={
        "type": "object",
        "required": ["name", "description", "body_markdown", "tool_dependencies"],
        "properties": {
            "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,63}$"},
            "description": {"type": "string", "minLength": 50, "maxLength": 600},
            "body_markdown": {"type": "string", "minLength": 100},
            "tool_dependencies": {
                "type": "array", "items": {"type": "string"}, "minItems": 1,
            },
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["filename", "content"],
                    "properties": {
                        "filename": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            },
        },
    },
)
```

## Validation

Three deterministic passes. All must pass for `status = "published"`.
Failures are written as `<skill-id>.diagnostic.json` next to where the
package would have lived.

| Pass | Check |
|---|---|
| spec-conformance | YAML frontmatter parses; required keys present; `name` matches `^[a-z][a-z0-9-]{1,63}$`; `description` length in [50, 600]. |
| tool-grounding | Every backticked or referenced tool name in body and in `tool_dependencies` resolves in the harvested tool set for `(server_id, source_hash)`. |
| length-bounds | Body length in [100, 8000] chars; reference files individually within [50, 12000] chars. |

The tool-grounding pass is the cheap stand-in for the design's full
Atlas-grounding pass. It catches the dominant LLM failure mode
(invented tool names) without an Atlas.

## Configuration

```toml
[skill_generation]
enabled = false                      # global default
chunk_size = 12                      # used upstream by use-case mining
prompt_version = "v1"
cluster_threshold = 0.78
max_synthesis_concurrency = 4
output_dir = ".mcp_semantic_gateway"
description_min_chars = 50
description_max_chars = 600
body_min_chars = 100
body_max_chars = 8000
```

## Wiring into existing code

Generated skills are written to
`.mcp_semantic_gateway/skills/<server_id>/<source_hash[:12]>/<skill-id>/v1/`.
For retrieval to surface them, the user adds a `Skill`-type entry to
`config.toml`:

```toml
[servers.generated-skills]
type = "skill"
path = ".mcp_semantic_gateway/skills"
enabled = true
```

The existing `Collector.collect_skills()` already walks the directory
recursively for `SKILL.md` files. No retrieval-side code changes
required. The CLI helper `mcp-semantic-gateway synth init-skill-source`
adds this server entry automatically when missing (task below).

## Observability

Stage events emitted by the same `EventEmitter` defined in
[use-case-synthesis.md](use-case-synthesis.md):

- `clustering_started`, `clustering_completed`
- `skill_synthesis_started`, `skill_cache_hit`,
  `skill_synthesized`, `skill_validated`, `skill_rejected`,
  `skill_written`
- `synthesis_completed`, `synthesis_failed`

Each event carries `run_id`, `cluster_id` where applicable, token
usage from the LLM call, and validation report on rejection events.

Run summary appended to the use-case-mining summary printed by the
`synth` CLI command:

```
Skills generated:        9
Skills cached:           3
Skills rejected:         2 (tool_grounding=2)
Diagnostics:             .mcp_semantic_gateway/diagnostics/synthesis/run-7f3a/
```

## Idempotency and re-runs

The cache key `(server_id, source_hash, cluster_hash, model_id, prompt_version)`
is computed before each synthesis call. A hit means the skill on disk
is already up-to-date; the pipeline skips the LLM call.

Bumping `prompt_version` invalidates skill cache without invalidating
the upstream use-case cache (use cases are still valid; only the
synthesis prompt changed).

Bumping `source_hash` invalidates everything downstream of the source.
Stale `<source_hash[:12]>` directories are NOT auto-deleted in v1; a
`mcp-semantic-gateway synth gc` task can prune them later.

## CLI surface

Reuses the `synth` command from
[use-case-synthesis.md](use-case-synthesis.md):

```
mcp-semantic-gateway synth                # use-case mining + skill synthesis
mcp-semantic-gateway synth --skills-only  # cluster + synthesize from existing use cases
mcp-semantic-gateway synth status         # also lists generated skills per source
mcp-semantic-gateway synth init-skill-source  # ensure config has a skill-type server entry
```

## Tasks

Implementation order. Depends on
[use-case-synthesis.md](use-case-synthesis.md) Phases A–D being done.

### Phase G — Clustering

- [ ] `ingestion/skill_clusterer.py` — `UseCaseCluster`, `cluster_use_cases(records, threshold)`.
- [ ] Greedy agglomerative algorithm with cosine similarity over `LocalEmbedder` outputs.
- [ ] Medoid selection for `centroid_description`.
- [ ] `cluster_hash` deterministic from sorted members.
- [ ] Unit tests: identical descriptions cluster together; orthogonal descriptions stay split; threshold tuning behaves monotonically.

### Phase H — Synthesis

- [ ] `ingestion/skill_synthesizer.py` — `SkillPackage` dataclass, `synthesize_skill(cluster, harvested_tools, llm)`.
- [ ] Synthesis tool-use schema (`emit_skill_package`).
- [ ] System + user prompt templates (`prompt_version = "v1"`).
- [ ] Per-cluster cache lookup before LLM call.
- [ ] Concurrency bound via shared semaphore with use-case mining.
- [ ] Integration test: stub LLM emits a fixed package; expected `SKILL.md` and `.meta.json` produced in memory.

### Phase I — Validation

- [ ] `ingestion/skill_validator.py` — `ValidationReport`, three-pass deterministic validator.
- [ ] Tool-grounding pass: backtick/inline reference extraction + name resolution.
- [ ] Diagnostic writer for rejected skills.
- [ ] Unit tests: valid package passes; hallucinated tool name fails grounding; oversize body fails length-bounds.

### Phase J — Cache writer

- [ ] `ingestion/skill_writer.py` — `write_package(package, project_root) -> Path`.
- [ ] Path layout per S-2; `<skill-id>` collision suffixing.
- [ ] Atomic write (temp file + rename) so partial writes don't appear to `Collector.collect_skills()`.
- [ ] `references/<group>.md` files written when synthesizer emits them.
- [ ] Unit tests: layout correct; idempotent rewrites byte-stable; collision suffixing.

### Phase K — Wiring and CLI

- [ ] `cli/main.py` — `synth --skills-only` flag (skip mining, run clustering+synthesis on existing use cases).
- [ ] `cli/main.py` — `synth status` extension to list generated skills per source.
- [ ] `cli/main.py` — `synth init-skill-source` to add the `Skill`-type server entry idempotently.
- [ ] Smoke test: end-to-end with stub LLM — OpenAPI fixture → use cases → clusters → skills on disk → re-run `index` finds them via existing `Collector.collect_skills`.

### Phase L — Observability deltas

- [ ] Skill-stage event types added to `EventEmitter`.
- [ ] Run summary extended to include skill counts.
- [ ] Diagnostic file format documented in this doc.

### Phase M — Documentation and exit

- [ ] Update `README.md` with the project-local cache layout and the
      `Skill`-type server entry users need to add (or invoke
      `init-skill-source`).
- [ ] Update this doc's decisions log with deltas observed during
      implementation.
- [ ] Confirm cluster-threshold default 0.78 is appropriate against the
      first real source; record tuning data.

## Open questions

- **Cluster threshold calibration** — 0.78 is a starting guess.
  Validate against the first real OpenAPI source (e.g. GitHub) and
  record the tuned value.
- **`allowed-tools` derivation when body cites optional tools** — if
  the body says "you *may* use X", does X go into `allowed-tools`?
  Default for v1: yes, the union of every cited tool. Conservative.
- **Stale cache pruning** — `synth gc` task is named but unspecified;
  defer until first user reports clutter.
- **Reference file emission** — the schema permits `references/`
  files but the v1 prompt template encourages a single SKILL.md. Add
  references when the cluster spans multiple `group_label`s upstream.

## What this design intentionally does NOT cover

- Pattern-attribution (`pattern_dependencies` in `.meta.json`).
- Section-level surgical updates and stable section anchors.
- Quarantine, supersession, rollback state machines.
- Feedback aggregation / negative-signal-driven re-synthesis.
- HyDE two-vector descriptions and Qdrant migration.
- LLM-judge discriminator pass.
- Cross-source skill bundling.
