---
name: mcp-gateway-run-tests
description: Use when running, debugging, or extending the test suite for the mcp-semantic-gateway repository. Covers pytest invocation, the e2e harness, fixtures, the stub LLM, and how unit tests are organized per pipeline stage.
---

# Test the gateway

`pytest` with `pytest-asyncio` (auto mode). Tests live under `tests/`.

## Run

```bash
make test                            # everything
uv run pytest tests/test_e2e.py      # one file
uv run pytest -k "skill_writer"      # by name pattern
uv run pytest -x --ff                # stop on first failure, prioritize last failed
uv run pytest -vv -s                 # verbose + show stdout (debug prints)
```

Coverage:

```bash
uv run pytest --cov=mcp_semantic_gateway --cov-report=term-missing
```

## Test layout

| File | What it covers |
|---|---|
| `test_e2e.py` | Real proxy against stub MCP + OpenAPI upstreams; the smoke test of last resort. |
| `test_executor_e2e.py` | `OpenAPIExecutor` against an in-memory FastAPI petstore. |
| `test_forge.py` | OpenAPI → MCP tool transformation (`integration/`). |
| `test_openapi_ingestion.py` | Spec parsing, operation harvesting. |
| `test_openapi_executor.py` | Auth shapes, request building, response mapping. |
| `test_mcp_client.py` | Native MCP server stdio client. |
| `test_registry.py` | SQLite tool registry behavior, namespacing. |
| `test_chunker.py` | Tool-list chunking for synth. |
| `test_use_case_miner.py` | Stage 1 of synth (LLM mining + grounding). |
| `test_skill_clusterer.py` | Stage 2 (cosine clustering, medoid selection). |
| `test_skill_synthesizer.py` | Stage 3 (cluster → SKILL.md). |
| `test_skill_validator.py` | Three-pass validation gate. |
| `test_skill_writer.py` | On-disk layout + cache key. |
| `test_skills_ingestion.py` | Reading hand-authored skill libraries. |
| `test_get_skill.py` | The `get_skill` tool surface on the proxy. |
| `test_llm_abstraction.py` | Anthropic + OpenAI-compatible provider parity. |
| `test_observability.py` | Structured event emission. |

## Stubbing the LLM

`tests/_stub_llm.py` provides a deterministic fake LLM client for synth tests — never hits a real provider. Reach for it whenever a new test would otherwise need network access. New synth tests should follow the same pattern: stub the LLM, assert on the structured output and the cache key.

## Fixtures worth knowing

`tests/fixtures/` holds canned OpenAPI specs, hand-authored skills, and MCP server transcripts used across multiple test files. Prefer adding to existing fixtures over inlining new YAML/JSON in test bodies.

## Writing new tests

- **Async tests use the function-style decorator.** `@pytest.mark.asyncio` (mode is `auto` so it is implicit, but stating it is fine).
- **Don't hit the real disk** unless the test is explicitly about on-disk layout — use `tmp_path`.
- **Don't hit the network.** If a new code path needs an HTTP client, inject it via constructor and pass a stub.
- **One assertion per behavior.** It is fine to have many `assert` lines; just keep each test focused on one observable outcome.

## E2E sanity check

When making changes that touch the proxy, the registry, or the OpenAPI executor, run `test_e2e.py` last:

```bash
uv run pytest tests/test_e2e.py -vv
```

It is slower than the unit tests but catches integration regressions the per-module tests miss.
