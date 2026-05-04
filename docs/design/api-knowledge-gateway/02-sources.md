# 02 — Sources

A **source** is anything we ingest. The previous design conflated
"how to fetch" and "how to parse" inside a single `SourceAdapter`
Protocol. This design splits them.

## Why split

Real-world ingestion has a small fetch matrix and a small parse matrix,
but their cross product is the actual configuration space:

|              | OpenAPI | Godot-XML | Sphinx HTML | `.d.ts` | Markdown |
|---           |---      |---        |---          |---      |---       |
| Local path   | ✓       | ✓         | ✓           | ✓       | ✓        |
| HTTP URL     | ✓       | (rare)    | ✓           | ✗       | ✓        |
| Git ref      | ✓       | ✓         | ✓           | ✓       | ✓        |
| npm/PyPI pkg | ✗       | ✗         | ✗           | ✓       | ✓        |

Bundling fetch and parse into one adapter forces 5×4 = 20 adapters in the
worst case. Splitting them makes it 5 + 4 with a registry that composes
on demand.

A second reason: **local/remote parity is a first-class principle**
(decision N-2). When the user develops against a local copy of a doc
site and later switches to remote, only the `Acquirer` changes. The
`Parser` is unchanged, so the resulting Atlas is byte-identical (modulo
acquisition timestamps in metadata).

## SourceRef

```python
class LocalPathSourceRef(BaseModel):
    kind: Literal["local-path"] = "local-path"
    path: str                              # absolute or workspace-relative

class HTTPSourceRef(BaseModel):
    kind: Literal["http"] = "http"
    url: str
    etag: str | None = None
    headers: dict[str, str] = {}           # for auth, user-agent, etc.

class GitSourceRef(BaseModel):
    kind: Literal["git"] = "git"
    repo: str                              # https or git@
    ref: str = "HEAD"                      # tag, branch, sha
    subpath: str | None = None
    sparse: bool = True                    # sparse-checkout subpath only

SourceRef = LocalPathSourceRef | HTTPSourceRef | GitSourceRef
```

`SourceRef` is the only thing parsers see (via `RawSnapshot.source_ref`).
Parsers use it for diagnostic context, never for fetching.

## Acquirer

An `Acquirer` is responsible for a single `SourceRef` kind. It knows
nothing about the content's format.

```python
class Acquirer(Protocol):
    supported_kinds: set[str]              # subset of {'local-path', 'http', 'git', ...}

    async def acquire(self, ref: SourceRef) -> RawSnapshot:
        """Fetch bytes. Idempotent. Content-hashed."""

    async def detect_changes(self, prior: RawSnapshot) -> ChangeSignal:
        """Cheap freshness check (HEAD request, git ls-remote, mtime)."""
```

### Built-in acquirers

| Acquirer | Kinds | Behavior |
|---|---|---|
| `LocalPathAcquirer` | `local-path` | Reads file or directory, content-hashes raw bytes. Watches mtime for `detect_changes`. |
| `HTTPAcquirer` | `http` | `If-None-Match` / `If-Modified-Since`. Caches response by ETag + URL. Auth header from `SourceConfig.auth`. |
| `GitAcquirer` | `git` | Clone (sparse if `subpath` set) into `.mcp_semantic_gateway/cache/git/<repo-hash>`. `git ls-remote` for `detect_changes`. |
| `LocalProjectAcquirer` | `local-project` | Reads the user's project root (configured path or CWD). Used by `LocalCodeGraph`, not by primary source ingestion. Walks the tree respecting `.gitignore`. Content-hashes file contents. |
| `GitHubCodeSearchAcquirer` | `public-corpus-github` | Calls GitHub Code Search API per Atlas entity qualified name. Rate-limit-bucketed; auth via `GITHUB_TOKEN`. Caches by `(query, page, etag)`. License-filters results before returning. |
| `NpmAcquirer` (optional) | `npm` | `npm pack` + extract; pins on package version. |
| `PyPIAcquirer` (optional) | `pypi` | sdist download + extract. |

`LocalPathAcquirer`, `HTTPAcquirer`, and `GitAcquirer` are required for
primary source ingestion. `LocalProjectAcquirer` and
`GitHubCodeSearchAcquirer` are required for the v0 `LocalCodeGraph` and
`PublicCorpusGraph` subjects respectively (see [04](04-code-graph.md)).
Others are added as demand appears.

### Code-graph acquirers vs. source-ingest acquirers

Acquirers used by code-graph subjects (`LocalProjectAcquirer`,
`GitHubCodeSearchAcquirer`) operate under the same `Acquirer` Protocol
but have **different freshness models** from source-ingest acquirers:

| Acquirer | Freshness model |
|---|---|
| `LocalProjectAcquirer` | Mtime-driven; rebuilds the local code graph on file changes within a debounce window. |
| `GitHubCodeSearchAcquirer` | TTL-driven (default 7 days per query); refresh per Atlas snapshot or operator-triggered. |
| Source-ingest acquirers | Snapshot-driven; rebuilt only on Atlas snapshot rotation. |

These differences are absorbed at the `CodeGraph` subject layer; the
`Acquirer` Protocol itself is unchanged.

### `RawSnapshot` shape

```python
class RawSnapshot(BaseModel):
    source_ref: SourceRef
    raw_hash: str                          # sha256 over canonicalized artifacts
    artifacts: dict[str, bytes]            # logical filename -> bytes
    metadata: dict[str, Any] = {}          # acquirer-specific (etag, git sha, etc.)
    acquired_at: datetime
```

Some sources are single-file (an OpenAPI JSON). Some are multi-file
(`doc/classes/*.xml` for Godot). `artifacts` is always a dict, keyed by
logical filename, so the same parser handles both shapes. Acquirers
canonicalize order before hashing.

## Parser

```python
class Parser(Protocol):
    content_kind: str                      # 'openapi' | 'godot-xml' | 'sphinx' | '.d.ts' | ...

    async def parse(self, raw: RawSnapshot) -> AtlasSnapshot:
        """Deterministic. Same RawSnapshot → identical AtlasSnapshot
        (modulo created_at)."""

    def partition_hint(self, snapshot: AtlasSnapshot) -> PartitionMap: ...
    def extract_symbols(self, text: str) -> list[SymbolRef]: ...
    def detect_version(self, raw: RawSnapshot) -> SourceVersion: ...
    def classify_entity_change(self, prior: Entity | None, current: Entity | None) -> ChangeClass: ...

    tool_requirements: dict[EntityKind, list[str]]
```

Determinism is the cache contract. Non-determinism breaks content
hashing and surgical updates.

### Built-in parsers

| Parser | content_kind | Source ships | Notes |
|---|---|---|---|
| `OpenAPIParser` | `openapi` | structured doc tree | resolves `$ref`, extracts examples, partitions by tag |
| `GodotXMLParser` | `godot-xml` | structured doc tree + GDScript code blocks | partitions by class |
| `SphinxParser` | `sphinx` | structured HTML + Python AST in code blocks | partitions by module |
| `DTSParser` | `.d.ts` | TypeScript AST | partitions by namespace/module |
| `MarkdownParser` | `markdown` | unstructured prose | last-resort; symbol extraction only |

Each parser declares the **AST availability** for its content kind. This
is the input to [04-code-graph.md](04-code-graph.md):

| Parser | AST availability |
|---|---|
| `OpenAPIParser` | doc tree + ref graph; AST only over embedded code samples (rare) |
| `GodotXMLParser` | doc tree + ref graph; AST over GDScript example blocks |
| `SphinxParser` | doc tree + ref graph; full Python AST over example blocks; can opt-in to parse the source if the subpath is declared |
| `DTSParser` | TypeScript AST over the entire source |
| `MarkdownParser` | symbol extraction only |

## Source composition

```python
class Source(BaseModel):
    id: str                                # 'godot', 'stripe', etc.
    ref: SourceRef
    parser_kind: str                       # 'openapi' | 'godot-xml' | ...
    auth: AuthConfig | None = None

    @property
    def acquirer(self) -> Acquirer:
        return acquirer_registry.lookup(self.ref.kind)

    @property
    def parser(self) -> Parser:
        return parser_registry.lookup(self.parser_kind)
```

`Source` is dataclass-shaped. The `Acquirer` and `Parser` are resolved
through registries at use time. A new combination — say,
`(LocalPathAcquirer, OpenAPIParser)` for a vendored OpenAPI spec — is
zero new code.

## Source version detection

```python
class SourceVersion(BaseModel):
    semantic: str | None                   # '4.4.1' if available
    raw_hash: str                          # always present
    major: int                             # parsed from semantic, else 0

    @property
    def is_versioned(self) -> bool:
        return self.semantic is not None
```

Parsers implement `detect_version`. Strategies:

- **OpenAPI** — read `info.version`.
- **Godot XML** — read `<class version="...">` attribute on any class file.
- **Sphinx** — read `release` from conf.py if discoverable; otherwise
  `metadata` from rendered HTML; else fall back to raw hash.
- **`.d.ts`** — npm package version from manifest.
- **Markdown** — raw hash only.

For unversioned sources, the parser declares whether to treat changes as
"always minor" (additive APIs) or to bump major on content-hash distance
threshold. This decision is made per parser, not per acquirer.

## Locality matrix — full coverage

The combinations the v1 design must support out of the box:

| Source                          | Acquirer | Parser |
|---|---|---|
| Public OpenAPI spec (URL)       | `HTTPAcquirer` | `OpenAPIParser` |
| Vendored OpenAPI spec           | `LocalPathAcquirer` | `OpenAPIParser` |
| Godot engine (git ref)          | `GitAcquirer` | `GodotXMLParser` |
| Godot engine (local checkout)   | `LocalPathAcquirer` | `GodotXMLParser` |
| Sphinx site (URL)               | `HTTPAcquirer` | `SphinxParser` |
| Sphinx site (local `_build`)    | `LocalPathAcquirer` | `SphinxParser` |
| Markdown corpus (git)           | `GitAcquirer` | `MarkdownParser` |
| Markdown corpus (local)         | `LocalPathAcquirer` | `MarkdownParser` |

`.d.ts` and npm/PyPI are post-v1; the registry pattern accommodates them.

## On adapters being gone

The previous design's `SourceAdapter` Protocol is replaced by the
`(Acquirer, Parser)` composition. Anything an adapter previously owned
moves to one of the two:

| Old adapter responsibility | Now owned by |
|---|---|
| `acquire()` | `Acquirer` |
| `parse()` | `Parser` |
| `partition_hint()` | `Parser` |
| `detect_version()` | `Parser` (reads from `RawSnapshot`) |
| `classify_entity_change()` | `Parser` |
| `extract_symbols()` | `Parser` |
| `tool_requirements` | `Parser` |

The OpenAPI adapter from the existing `forge.py` module is decomposed
along these lines — `OpenAPIParser` keeps the parsing logic;
acquisition is generic.

## Open implementation choices

- **Authenticated acquirers.** `HTTPAcquirer` and `GitAcquirer` support
  per-source `AuthConfig`. The shape is reused for runtime auth
  pass-through (see [07](07-knowledge-mcp-surface.md)). v1 supports
  bearer + basic; sigv4 / OAuth flows are post-v1.
- **Acquirer rate limiting.** Public doc sites may impose limits;
  `HTTPAcquirer` should ship a token-bucket throttle config field. v1
  default: 5 req/sec per host.
- **Sparse git checkouts.** Default to sparse for `git` refs with a
  declared `subpath`. Full clones are an opt-in for parsers that need
  the entire repo (e.g., a `LocalCodeGraph`-style mining of the repo's
  test directory).
