# 06 — Caching and Surgical Updates

The cost model for "ingest any public API" only works if minor source
revisions are cheap. A naive pipeline that re-synthesizes every skill on
every patch release is unaffordable at the Godot scale. This document
specifies how the system avoids that.

## Principles

- **Hash everything that's a function of stable input.** Atlas entities,
  partition groups, snapshot fingerprints, skill outputs. Hashes are the
  cache keys.
- **Surgical, not nuclear.** When a source changes, identify the smallest
  set of skill sections affected and regenerate only those.
- **Safe over clever.** When a change is breaking, quarantine and require
  fresh synthesis. No automated repair attempts in v1 (decision 16).

## Hash hierarchy

Defined in [01](01-atlas.md), restated for context:

```
entity_hash    = sha256(canonical_json(name, qualified_name, signature, params, return_type, doc, deprecated))
group_hash     = sha256(sorted(entity_hashes_in_group))
snapshot_hash  = sha256(sorted(entity_hashes) + sorted(edge_serializations))
skill_content_hash = sha256(SKILL.md + sorted(section_files))
```

`.meta.json` records the entity hashes a skill depended on at generation
time. The diff between *that* set and the *current* Atlas snapshot drives
everything below.

## Re-ingest flow

When a Source Adapter is told to refresh:

```
1. acquire(config)            → RawSnapshot
2. compute raw_hash
3. if raw_hash matches an existing AtlasSnapshot → cache hit; exit.
4. parse(raw)                 → AtlasSnapshot (new)
5. compute snapshot_hash, source_major
6. classify source bump:
     a. unchanged source_major → minor/patch path  → "Surgical update"
     b. source_major increased → "Major bump"
7. persist new AtlasSnapshot
8. follow appropriate update path
```

## Surgical update path

For minor and patch bumps, lineage carries forward and most skills should
be cheap to update or fast-forward.

```
For each published skill in (source, prior_snapshot):
    new_dep_hashes = lookup current entity_hashes for skill.atlas_dependencies
    diff = compute_diff(skill.atlas_dependencies, new_dep_hashes)

    if diff.changed_entities is empty:
        action = "fast-forward"
    elif diff.changed_entities ⊆ removable_changes:        # see "Change classification"
        action = "surgical"
    else:
        action = "quarantine"
```

### Action: fast-forward

The skill's referenced entities are unchanged. No regeneration.

- Hard-link or copy the skill package directory from the prior
  source-version path to the new source-version path.
- Insert a new row in `skills` with `snapshot_id` = new snapshot, same
  `skill_version`, same `lineage_root`, `parent_version` = prior version.
- Re-index the Qdrant point with updated `source_version` payload.

Cost: zero LLM calls. Pure file/DB work.

### Action: surgical

One or more entity hashes changed in non-breaking ways. Only the affected
sections are regenerated.

```
1. dirty_groups = {group_id for each section in skill.sections
                   if any(e in diff.changed_entities for e in section.entities)}
2. For each dirty group:
       a. Re-extract reference content from new Atlas entities.
       b. Re-synthesize examples that depend on changed entities.
       c. If a SKILL.md fragment section is dirty, regenerate that fragment.
3. Bump skill_version (parent_version = prior, lineage_root unchanged).
4. Run the discriminator on the updated package.
5. If discriminator passes → publish; if fails → mark draft and log.
```

Section files that are not dirty are byte-preserved. SKILL.md fragments
not in dirty sections are byte-preserved. The atomic unit of regeneration
is a section, not the whole skill.

Cost: typically 1–N LLM calls where N = dirty sections. Empirically
bounded — most patch releases touch <5% of the surface, so most skills
are fast-forwards and a few are surgical.

### Action: quarantine

Some entity changed in a way that breaks the skill's grounding (signature
mismatch, removed entity, type change). The discriminator's grounding pass
would fail.

- Skill row's `status` flips to `quarantined`.
- Qdrant point is removed from the `skills` collection (so retrieval
  cannot return it).
- A `quarantine_reason` field in `.meta.json` records the trigger entities.
- The skill remains on disk for inspection / manual revival.

The planner's gap-policy handles quarantined skills the same as missing
skills (decision 19).

A scheduled or operator-triggered job can reprocess quarantined skills
through full re-synthesis. v1 does not auto-reprocess.

## Change classification

A change to an entity falls into one of three buckets. The classifier is
deterministic and lives alongside each Source Adapter (since "what
constitutes breaking" varies by source kind — adding an optional REST query
param is non-breaking; adding a required Godot constructor argument is
breaking).

### Non-breaking (cosmetic)

- Doc text changed without semantic shift (whitespace, typo fix, link
  canonicalization).
- `since_version` updated.
- Examples added/changed (we use them as inputs, but their change doesn't
  invalidate downstream skill content).

→ Treated as **fast-forward** even when entity_hash changes (the hash
includes doc text). To support this, the classifier may produce a
`semantic_hash` excluding cosmetic fields; surgical updates compare
semantic hashes for change classification while still recording the full
hash.

### Non-breaking (additive)

- New optional parameter added.
- New method added to a class.
- New entity added to a partition group.

→ **Surgical** if the changed entity is a dependency, else **fast-forward**.

### Breaking

- Method signature changed (parameter added/removed/renamed/retyped).
- Entity removed.
- Inheritance chain altered.
- Return type changed.

→ **Quarantine** if any dependency is affected.

The classifier's rules are encoded per Source Adapter as a function:

```python
class ChangeClass(Enum):
    COSMETIC = "cosmetic"
    ADDITIVE = "additive"
    BREAKING = "breaking"

class SourceAdapter:
    def classify_entity_change(
        self, prior: Entity | None, current: Entity | None
    ) -> ChangeClass: ...
```

## Major bump path

When the source major version increases:

1. Mark all skills under the prior `<source-major>/` as `superseded` (they
   stay on disk for reference and rollback).
2. Trigger full cold regeneration:
   - Re-cluster the new Atlas.
   - Re-generate use cases (priors from prior major are passed in as soft
     hints, not authoritative templates).
   - Re-synthesize skills from scratch. `skill_version = 1`,
     `lineage_root = 1`, `parent_version = null`.
3. Carry forward negative-feedback **notes** from prior major as known-issue
   hints in synthesis prompts. Do **not** carry forward `feedback_score`
   as a ranking signal.

A major bump is the rare expensive event. We accept the cost because the
alternative — silently letting old skills drift across a major boundary
— is unsafe.

## Cache layers

Beyond the surgical-update yield, several smaller caches:

- **Raw acquisition cache.** `Source.acquire()` keys on the source's
  upstream identifier (URL, git ref, etc.) and content-hashes the result.
  Re-fetching identical content is free.
- **HyDE rewrite cache.** Per-query text + source-set. TTL: 24 hours.
- **Use-case cluster cache.** Cluster outputs key on `cluster_hash`. A
  cluster whose entity set didn't change does not re-generate.
- **Embedding cache.** Embedding the same string twice is free; key is
  text + model id.

All caches are content-addressed and survive process restart.

## Observability

The pipeline emits per-snapshot statistics so we can see how the cache is
performing:

```
SnapshotIngestReport {
    snapshot_id: string
    total_skills: int
    actions: { fast_forward: int, surgical: int, quarantine: int, regenerate: int }
    llm_calls: int
    tokens_in: int
    tokens_out: int
    duration_seconds: float
}
```

If `surgical + regenerate` is climbing for "minor" bumps, the change
classifier's heuristics are too pessimistic and need tuning.

## Open implementation choices

- **Semantic vs. full entity hash.** Whether to track both, or treat
  cosmetic-only diffs as "fast-forward by classifier override." Both work;
  separate hashes are cleaner for debugging. Lean: ship both.
- **Quarantine reprocess scheduling.** Manual-only in v1. A nightly
  reprocess job is a Phase-4+ ergonomics improvement.
- **Cross-source dependency invalidation.** If skill A in source X
  references entities from source Y (rare but possible — e.g., an OpenAPI
  skill that wraps a database client), Y's changes invalidate A. v1
  doesn't model cross-source deps; can be added by extending
  `atlas_dependencies` with `source_id` per entry.
