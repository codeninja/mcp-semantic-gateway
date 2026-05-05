---
name: mcp-gateway-release-publish
description: Use when cutting a new release of mcp-semantic-gateway — bumping the version in pyproject.toml, building the wheel, publishing to PyPI, or troubleshooting the publish step. Triggers on "release", "publish", "bump version", "new version", "PyPI".
---

# Release & publish

`mcp-semantic-gateway` is published to PyPI. The build backend is `uv_build`; releases are cut via `uv build` + `uv publish`.

## Versioning

Single source of truth: `pyproject.toml` `[project] version = "..."`. SemVer.

- **Patch (`0.2.x`)** — bug fixes, additions to bundled skills, internal refactors.
- **Minor (`0.x.0`)** — new CLI commands, new source types, new public APIs.
- **Major (`x.0.0`)** — breaking changes to `config.toml` schema, MCP tool surface, or on-disk layout.

The `__version__` in `src/mcp_semantic_gateway/__init__.py` is read by the `--version` flag — keep it in sync if it is hard-coded there.

## Cut a release

```bash
# 1. Bump version
$EDITOR pyproject.toml             # bump [project] version

# 2. Lockfile + sanity
uv sync
uv run pytest
uv run ruff check .

# 3. Commit, tag
git commit -am "bump"
git tag v$(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2)
git push origin main --tags

# 4. Build + publish
make build-and-publish
```

`make build-and-publish` runs:

```
rm -rf dist/ && uv build && uv publish
```

`uv publish` reads credentials from `UV_PUBLISH_TOKEN` (a PyPI API token) or `~/.pypirc`. Never commit either.

## Verify

```bash
# In a clean venv:
pip install --upgrade mcp-semantic-gateway
mcp-semantic-gateway --version       # confirms new version
mcp-semantic-gateway init            # smoke test
```

## What ships in the wheel

`uv_build` bundles everything under `src/mcp_semantic_gateway/` automatically, including:

- The Python source.
- `py.typed` marker.
- The `skills/` tree (consumer + development skills) shipped to `onboard` targets.

Confirm before pushing:

```bash
uv build
unzip -l dist/mcp_semantic_gateway-*.whl | grep SKILL.md
```

`uv_build` bundles every file under `src/mcp_semantic_gateway/` automatically, including non-Python data files like `SKILL.md`. No `__init__.py` is needed in skill directories — they ship as resource trees and are read at runtime via `importlib.resources`. Still, verify the wheel listing on every release; an accidental `.gitignore` entry or stray exclude rule has cost teams a release before.

## Pre-release / dry-run

```bash
uv publish --dry-run                 # validate tokens + metadata without uploading
```

## Yanking a bad release

PyPI doesn't allow re-using a version. If a release ships broken:

1. Yank on PyPI (web UI, project → release → "Yank release"). Yanked versions install only when explicitly requested.
2. Bump to the next patch and publish a fix.

Never republish the same version with edits — yank and bump.
