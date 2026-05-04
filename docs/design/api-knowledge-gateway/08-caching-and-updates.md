# 08 — Caching and Updates

The cost model only holds if minor source revisions are cheap. This
document specifies the cache hierarchy, the surgical-update decision
tree, and how patterns + skills are kept in sync as sources evolve.

The mechanism is largely inherited from the prior design
([../api-introspection-skills/06-caching-and-updates.md](../api-introspection-skills/06-caching-and-updates.md)),
extended to cover patterns and to reflect the modular split.

## Hash hierarchy (consolidated)

```
entity_hash       = sha256(canonical_json(name, qualified_name, signature, params, return_type, doc, deprecated))
semantic_hash     = entity_hash with cosmetic fields excluded
group_hash        = sha256(sorted(entity_hashes_in_group))
snapshot_hash     = sha256(sorted(entity_hashes) + sorted(edge_serializations))
pattern_hash      = sha256(canonical_json(kind, participants, evidence_summary))
skill_content_hash = sha256(SKILL.md + sorted(section_files))
```

- **`entity_hash`** drives entity-level cache invalidation.
- **`semantic_hash`** drives change classification (cosmetic vs.
  additive vs. breaking).
- **`group_hash`** drives section-level skill updates and miner scope.
- **`pattern_hash`** drives pattern-level cache.
- **`snapshot_hash`** is the snapshot fingerprint.

## Re-ingest flow

```
1. acquirer.acquire(ref)            → RawSnapshot (with raw_hash)
2. if raw_hash matches an existing AtlasSnapshot → cache hit; exit.
3. parser.parse(raw)                → AtlasSnapshot (new)
4. compute snapshot_hash, source_major
5. classify source bump:
     a. unchanged source_major → minor/patch path → "Surgical update"
     b. source_major increased → "Major bump"
6. persist new AtlasSnapshot
7. follow appropriate update path
```

Every step is deterministic, content-addressed, and resumable.

## Surgical update path

For minor and patch bumps:

```
diff = atlas.diff_snapshots(prior_id, current_id)

# Fast-forward when nothing depends on the diff:
for entity in diff.changed_entities:
    if entity.semantic_hash unchanged: classify as cosmetic → fast-forward path

# Re-run miners on the affected scope:
miner_scope = {e.id for e in diff.changed_entities} ∪ {neighbor hops}
patterns_to_update = patterns where any participant ∈ miner_scope

for pattern in patterns_to_update:
    if pattern.participants_unchanged_in_diff: keep
    elif pattern.participants_in additive_changes: re-mine the pattern
    elif pattern.participants_in breaking_changes: invalidate
       → discriminator may quarantine downstream skill if any references the pattern

# Skills (if skill-gen is online): same surgical decision tree as prior design.
```

The decision tree per **artifact** (entity / pattern / skill section):

| Change classification | Action |
|---|---|
| Cosmetic only | Fast-forward (no work) |
| Additive | Surgical re-generation of the artifact |
| Breaking | Quarantine the artifact; require manual review |

### Action: fast-forward

The artifact's referenced entities are semantically unchanged. No
regeneration. The new `AtlasSnapshot` is referenced; lineage continues.

Cost: zero LLM calls.

### Action: surgical

One or more dependencies changed in non-breaking ways. Only the
affected artifact is regenerated. For patterns: re-run the originating
miner on the narrowed scope. For skill sections: re-synthesize that
section only.

Cost: bounded per artifact.

### Action: quarantine

A breaking change broke the artifact's grounding. The artifact's
`status` flips to `quarantined`. Knowledge index removes the point
from the live collection. The artifact remains on disk for inspection.

No automated repair. Manual or scheduled re-generation only.

## Change classification

The classifier lives on each `Parser` (ownership change vs prior
design — adapters are gone, parsers own this responsibility now).

```python
class ChangeClass(Enum):
    COSMETIC = "cosmetic"
    ADDITIVE = "additive"
    BREAKING = "breaking"

class Parser(Protocol):
    def classify_entity_change(
        self, prior: Entity | None, current: Entity | None
    ) -> ChangeClass: ...
```

Per-parser rules:

| Change | OpenAPI | Godot-XML | Sphinx | `.d.ts` |
|---|---|---|---|---|
| Doc text edited | cosmetic | cosmetic | cosmetic | cosmetic |
| Required param added | breaking | breaking | breaking | breaking |
| Optional param added | additive | additive | additive | additive |
| Method removed | breaking | breaking | breaking | breaking |
| Return type changed | breaking | breaking | breaking | breaking |
| New method added | additive | additive | additive | additive |
| `since_version` updated | cosmetic | cosmetic | cosmetic | cosmetic |
| Inheritance chain altered | breaking | breaking | breaking | breaking |

These are defaults; parsers refine for source-specific cases.

## Major bump path

When `source_major` increases:

1. All artifacts under prior `<source-major>/` are marked
   `superseded`. They remain on disk for reference and rollback.
2. Cold regeneration:
   - Re-cluster the new Atlas.
   - Re-run all miners (Tier 1 + 2 + 3 if enabled) from scratch.
   - Re-generate use cases.
   - If skill-gen is online: re-synthesize skills, with prior major's
     content as soft prior (not template).
3. Negative-feedback notes from prior major are carried forward as
   "known issues to avoid"; quantitative scores are not.

A major bump is rare but expensive. The alternative — silently
drifting old artifacts across a major boundary — is worse.

## Cache layers

| Cache | Key | TTL |
|---|---|---|
| Acquirer raw cache | `(SourceRef.kind, SourceRef.uri)` + content hash | indefinite |
| HyDE rewrite cache | `hash(query + source_set + source_majors)` | 24h |
| Embedding cache | `hash(text + model_id)` | indefinite |
| Slice cache (per subject) | `hash(entity_id + snapshot_id + subject + components + budget)` | until subject's freshness expires |
| Merged-slice cache | `hash(entity_id + snapshot_id + subjects_set + budget)` | min of per-subject TTLs |
| Pattern miner cache | `merged_slice_hash` per (entity, miner, snapshot) | until any contributing subject expires |
| Use-case cluster cache | `cluster_hash` | until snapshot superseded |
| Plan cache (if planner online) | `hash(task + filter_context)` | 5 min |
| Local-project file cache | mtime + path | invalidated on file change (debounced) |
| Public-corpus query cache | `hash(query + license_allowlist)` | 7 days |

All caches are content-addressed and survive process restart.

### Multi-subject invalidation rules

When code-graph content changes, invalidation is **per subject**:

- **Ingested change** (Atlas snapshot rotation) → invalidates ingested
  references, all merged slices touching the changed entities, and
  patterns whose participants changed.
- **Local-project change** (file mtime delta after debounce) →
  invalidates local-project references for that file's symbols, and
  merged slices that included those references. Patterns with
  local-project evidence are flagged for re-evaluation; patterns
  without local-project evidence are unaffected.
- **Public-corpus expiry** (TTL hit) → flags public-corpus references
  for refresh on next miner run; existing patterns remain valid until
  refresh completes and shows divergence.

A change in one subject **does not** invalidate other subjects'
caches. This is what makes the multi-subject cost model tractable.

## Quarantine semantics

Quarantine is the safe response to breakage in the absence of
automated repair (decision I-12).

- Atlas entity quarantine: not a thing — entities are facts about
  what was parsed; they don't break.
- Pattern quarantine: patterns whose participants changed breakingly.
  Quarantined patterns are removed from the `patterns` collection
  but kept in SQLite for inspection. Resurrection requires re-mining
  on a snapshot where the breaking change is resolved (or accepted as
  a new pattern).
- Skill quarantine: skills whose `atlas_dependencies` include
  breaking changes. Same semantics as prior design.

A scheduled or operator-triggered job can reprocess quarantined
artifacts through full re-generation. Not auto-run in v1.

## Observability

```python
class SnapshotIngestReport(BaseModel):
    snapshot_id: str
    duration_seconds: float
    entities_added: int
    entities_changed_cosmetic: int
    entities_changed_additive: int
    entities_changed_breaking: int
    entities_removed: int
    patterns_action: dict[str, int]   # 'fast_forward', 'surgical', 'quarantine', 'regenerate'
    skills_action: dict[str, int] | None  # only when skill-gen is online
    llm_calls: int
    tokens_in: int
    tokens_out: int
```

If `surgical + regenerate` is climbing on minor bumps, the parser's
classifier is too pessimistic. The report is the calibration signal.

## What this layer does NOT do

- It does not auto-repair quarantined artifacts.
- It does not cross-source invalidate (a pattern referencing entities
  from two sources is not modeled in v1).
- It does not version raw acquirer outputs; the acquirer's content
  hash is sufficient.
- It does not unify equivalent patterns mined from different subjects.
  A co-occurrence pattern mined from `IngestedCodeGraph` and the same
  pattern mined from `PublicCorpusGraph` are stored as distinct records
  unless their `pattern_hash` collides (i.e., participants and
  evidence-summary match exactly). Cross-subject pattern unification
  is post-v1.

## Open implementation choices

- **Quarantine reprocess scheduler.** Manual-only in v1; nightly
  reprocess job is Phase-5+ ergonomics.
- **Cosmetic-vs-semantic threshold.** Some doc edits change meaning
  (e.g., changing a default value). The classifier uses heuristics;
  tightening requires per-parser refinement informed by real diffs.
- **Cross-source dependencies.** Future hook in `atlas_dependencies`
  to carry `source_id` per entry; deferred until a real cross-source
  case appears.
