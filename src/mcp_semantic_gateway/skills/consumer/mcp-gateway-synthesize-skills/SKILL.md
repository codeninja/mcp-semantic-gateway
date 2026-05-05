---
name: mcp-gateway-synthesize-skills
description: Use when the user wants to generate `SKILL.md` workflows from an OpenAPI / Swagger source via the mcp-semantic-gateway `synth` command, inspect the synth cache, debug rejections, or register the generated library so an agent can find it. Triggers on words like "synth", "generate skills", "skill synthesis", "use cases".
---

# Synthesize skills from a tool catalog

Tool descriptions tell an agent *what `createOrder` does*. They do not tell it *how to refund a customer*. The `synth` pipeline mines real-world use cases out of opted-in OpenAPI sources and emits agent-skills-spec `SKILL.md` packages keyed on intent.

## Prerequisites

1. A source with `generate_skills = true` in `~/.mcp_semantic_gateway/config.toml` (see `mcp-gateway-configure-sources`).
2. An LLM provider configured under `[llm]` — Anthropic native, or any OpenAI-compatible endpoint (OpenAI, OpenRouter, Gemini, Ollama, vLLM).
3. `mcp-semantic-gateway index` already run once so the tool registry is populated.

## Pipeline stages

The `synth` command runs five stages back-to-back. Each is cached on `(server, source_hash, chunk_hash, model, prompt_version)` — re-runs against unchanged inputs are zero-cost no-ops.

```
harvest → chunk → mine use cases → cluster → synthesize SKILL.md
```

1. **Mine.** One LLM call per chunk emits candidate use cases (*"refund a customer's most recent order"*) each linked to specific tool names. Hallucinated tool names are deterministically rejected.
2. **Cluster.** Use case descriptions are embedded and clustered by cosine similarity. Related intents collapse into one concept.
3. **Synthesize.** One LLM call per cluster produces a full `SKILL.md` (frontmatter, description, procedure, tool dependencies). Three validation passes gate publication: spec conformance, tool grounding, length bounds.

## Run it

```bash
# Mine + cluster + generate
mcp-semantic-gateway synth

# See what happened
mcp-semantic-gateway synth status

# Register the generated skills as a `type = "skill"` source
mcp-semantic-gateway synth init-skill-source

# Re-index so agents can find them via `find_skills`
mcp-semantic-gateway index
```

Generated skills land at:

```
~/.mcp_semantic_gateway/skills/<server>/<hash>/<id>/v1/SKILL.md
```

`init-skill-source` adds a `[servers.<server>_skills]` block with `type = "skill"` pointing at that root.

## Iterating on the prompts

Every synth run writes structured JSONL events under `~/.mcp_semantic_gateway/synth/runs/<timestamp>/`:

- `events.jsonl` — every stage transition, cache hit/miss, token spend
- `rejections.jsonl` — use cases or skills that failed validation, with reason
- `summary.json` — top-line counts shown by `synth status`

Bump `prompt_version` in the source's config block to invalidate the cache for that source only — useful when iterating on a system prompt without re-paying for already-validated chunks.

## Troubleshooting

- **Zero use cases mined** — usually means the LLM rejected every candidate as ungrounded. Check `rejections.jsonl` for `reason: "tool_not_found"` and confirm the tool registry is current (`index`).
- **A specific skill is wrong** — delete its `SKILL.md` directory under `~/.mcp_semantic_gateway/skills/<server>/...` and re-run `synth`. The cache will re-synthesize that cluster only.
- **Costs are higher than expected** — confirm the cache is hitting (`synth status` shows hit rate). A churning `prompt_version` or a non-deterministic spec hash will defeat caching.

Full design notes: `docs/design/use-case-synthesis.md` and `docs/design/skill-generation.md` in the repo.
