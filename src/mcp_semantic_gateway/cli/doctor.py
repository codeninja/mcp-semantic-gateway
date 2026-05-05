"""``mcp-semantic-gateway doctor`` — operator-facing diagnostic command.

Walks the gateway configuration and runtime state and reports each check
with an actionable remediation when it fails. Exits non-zero on any
failed check so it can be wired into CI / pre-flight scripts.

Checks are ordered cheap-to-expensive:

1. Config file parse + path resolution
2. Index / vector DB presence
3. Auth env vars for each OpenAPI source
4. OpenAPI spec reachability (HTTP, optional ``--no-network`` to skip)
5. Skill source path existence
6. Route metadata coverage for indexed OpenAPI tools

Each check renders to a Rich table; failures collect a remediation
hint. JSON output is available via ``--json`` for scripts.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from mcp_semantic_gateway.config.loader import (
    gateway_home,
    load_config,
)
from mcp_semantic_gateway.config.models import (
    AuthType,
    MCPSemanticGatewayConfig,
    SourceType,
)


Status = Literal["pass", "fail", "warn", "skip"]


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    remediation: Optional[str] = None


@dataclass
class DoctorReport:
    results: List[CheckResult] = field(default_factory=list)

    def add(
        self,
        name: str,
        status: Status,
        detail: str,
        remediation: Optional[str] = None,
    ) -> None:
        self.results.append(CheckResult(name, status, detail, remediation))

    def failure_count(self) -> int:
        return sum(1 for r in self.results if r.status == "fail")

    def to_dict(self) -> dict:
        return {
            "results": [
                {
                    "name": r.name,
                    "status": r.status,
                    "detail": r.detail,
                    "remediation": r.remediation,
                }
                for r in self.results
            ],
            "failures": self.failure_count(),
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_config_loadable(report: DoctorReport) -> Optional[MCPSemanticGatewayConfig]:
    """Stage 1: config file exists + parses + Pydantic validates."""

    home = gateway_home()
    config_path = home / "config.toml"

    if not config_path.exists():
        report.add(
            name="config-file",
            status="fail",
            detail=f"No config at {config_path}.",
            remediation=(
                "Run `mcp-semantic-gateway init` to create a starter "
                f"config at {config_path}."
            ),
        )
        return None

    try:
        config = load_config(config_path)
    except Exception as e:  # tomllib.TOMLDecodeError or pydantic.ValidationError
        report.add(
            name="config-file",
            status="fail",
            detail=f"Failed to load {config_path}: {e}",
            remediation=(
                "Open the config in an editor and fix the reported parse / "
                "validation error. Pydantic prints field paths in dotted form."
            ),
        )
        return None

    report.add(
        name="config-file",
        status="pass",
        detail=f"Loaded {config_path} ({len(config.servers)} server(s)).",
    )
    report.add(
        name="gateway-home",
        status="pass" if home.exists() else "warn",
        detail=f"Data dir: {home}"
        + ("" if home.exists() else " (does not exist yet)"),
        remediation=None
        if home.exists()
        else "Run `mcp-semantic-gateway init` to create the data directory.",
    )
    return config


def _check_index_present(report: DoctorReport, config: MCPSemanticGatewayConfig) -> None:
    """Stage 2: index files exist and metadata DB has rows."""

    home = gateway_home()
    metadata_db = home / "index" / "metadata.db"
    vector_db = home / "index" / "vectors.db"

    if not metadata_db.exists():
        report.add(
            name="index-metadata-db",
            status="fail",
            detail=f"Missing {metadata_db}.",
            remediation=(
                "Run `mcp-semantic-gateway index` to build the metadata "
                "database from your configured sources."
            ),
        )
    else:
        try:
            with sqlite3.connect(metadata_db) as db:
                cur = db.execute("SELECT COUNT(*) FROM tools")
                count = int(cur.fetchone()[0])
            if count == 0:
                report.add(
                    name="index-metadata-db",
                    status="warn",
                    detail=f"{metadata_db} exists but has 0 indexed items.",
                    remediation=(
                        "Add at least one source to `[servers]` in your "
                        "config and re-run `mcp-semantic-gateway index`."
                    ),
                )
            else:
                report.add(
                    name="index-metadata-db",
                    status="pass",
                    detail=f"{count} indexed item(s) in {metadata_db}.",
                )
        except sqlite3.DatabaseError as e:
            report.add(
                name="index-metadata-db",
                status="fail",
                detail=f"Cannot read {metadata_db}: {e}",
                remediation=(
                    f"Delete {metadata_db} and re-run "
                    "`mcp-semantic-gateway index` to rebuild a clean index."
                ),
            )

    if not vector_db.exists():
        report.add(
            name="index-vector-db",
            status="fail",
            detail=f"Missing {vector_db}.",
            remediation=(
                "Run `mcp-semantic-gateway index` — the metadata DB is only "
                "half the index. Without vectors.db, semantic search returns "
                "nothing."
            ),
        )
    else:
        report.add(
            name="index-vector-db",
            status="pass",
            detail=f"Found {vector_db}.",
        )


def _check_auth_env_vars(report: DoctorReport, config: MCPSemanticGatewayConfig) -> None:
    """Stage 3: every auth.type that names env vars actually has them set."""

    for server_id, sc in config.servers.items():
        if sc.type != SourceType.OPENAPI or sc.auth is None:
            continue
        if not sc.enabled:
            continue
        auth = sc.auth
        required: List[str] = []
        if auth.type == AuthType.API_KEY and auth.api_key_env:
            required.append(auth.api_key_env)
        elif auth.type == AuthType.BEARER and auth.bearer_token_env:
            required.append(auth.bearer_token_env)
        elif auth.type == AuthType.BASIC:
            if auth.basic_username_env:
                required.append(auth.basic_username_env)
            if auth.basic_password_env:
                required.append(auth.basic_password_env)

        missing = [name for name in required if not os.environ.get(name)]
        check_name = f"auth-env:{server_id}"
        if not required:
            report.add(
                name=check_name,
                status="skip",
                detail=f"server {server_id!r} uses auth.type={auth.type.value} (no env vars).",
            )
        elif missing:
            report.add(
                name=check_name,
                status="fail",
                detail=(
                    f"server {server_id!r} requires "
                    f"{', '.join(missing)} but they are unset."
                ),
                remediation=(
                    f"Export the missing variables before running the "
                    f"proxy, e.g. `export {missing[0]}=...`."
                ),
            )
        else:
            report.add(
                name=check_name,
                status="pass",
                detail=(
                    f"server {server_id!r}: {', '.join(required)} all set."
                ),
            )


async def _check_openapi_reachable(
    report: DoctorReport,
    config: MCPSemanticGatewayConfig,
    skip_network: bool,
) -> None:
    """Stage 4: each OpenAPI source URL responds with 2xx (HEAD or GET)."""

    openapi_servers = [
        (sid, sc)
        for sid, sc in config.servers.items()
        if sc.type == SourceType.OPENAPI and sc.enabled
    ]
    if not openapi_servers:
        return

    if skip_network:
        for sid, _ in openapi_servers:
            report.add(
                name=f"openapi-reachable:{sid}",
                status="skip",
                detail="--no-network was set; reachability not verified.",
            )
        return

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for sid, sc in openapi_servers:
            check_name = f"openapi-reachable:{sid}"
            if not sc.url:
                report.add(
                    name=check_name,
                    status="fail",
                    detail=f"server {sid!r} has type=openapi but no `url`.",
                    remediation=(
                        f"Set `servers.{sid}.url` in your config to the "
                        "URL of the OpenAPI / Swagger document."
                    ),
                )
                continue
            try:
                resp = await client.get(sc.url)
            except httpx.HTTPError as e:
                report.add(
                    name=check_name,
                    status="fail",
                    detail=f"server {sid!r}: GET {sc.url} failed — {e}",
                    remediation=(
                        "Verify the URL is reachable from this host (DNS, "
                        "TLS, VPN). If the spec lives behind auth, fetch "
                        "it manually and host it on a path the gateway "
                        "can read without credentials."
                    ),
                )
                continue
            if resp.status_code >= 400:
                report.add(
                    name=check_name,
                    status="fail",
                    detail=(
                        f"server {sid!r}: GET {sc.url} returned "
                        f"HTTP {resp.status_code}."
                    ),
                    remediation=(
                        "Confirm the URL points to the JSON / YAML spec "
                        "document directly (not a Swagger UI HTML page)."
                    ),
                )
            else:
                report.add(
                    name=check_name,
                    status="pass",
                    detail=(
                        f"server {sid!r}: GET {sc.url} -> "
                        f"HTTP {resp.status_code}."
                    ),
                )


def _check_skill_paths(report: DoctorReport, config: MCPSemanticGatewayConfig) -> None:
    """Stage 5: every skill source's `path` resolves to an existing dir."""

    for sid, sc in config.servers.items():
        if sc.type != SourceType.SKILL:
            continue
        if not sc.enabled:
            continue
        check_name = f"skill-path:{sid}"
        if not sc.path:
            report.add(
                name=check_name,
                status="fail",
                detail=f"server {sid!r} has type=skill but no `path`.",
                remediation=(
                    f"Set `servers.{sid}.path` to a directory containing "
                    "one or more `SKILL.md` packages."
                ),
            )
            continue
        path = Path(sc.path).expanduser()
        if not path.exists():
            report.add(
                name=check_name,
                status="fail",
                detail=f"server {sid!r}: {path} does not exist.",
                remediation=(
                    "Create the directory or correct the path. For "
                    "auto-generated skills, run "
                    "`mcp-semantic-gateway synth` first."
                ),
            )
        elif not path.is_dir():
            report.add(
                name=check_name,
                status="fail",
                detail=f"server {sid!r}: {path} is not a directory.",
                remediation=(
                    "Skill sources must point at a directory. Update the "
                    "path so it references the parent dir of your "
                    "`SKILL.md` packages."
                ),
            )
        else:
            skills = list(path.rglob("SKILL.md"))
            if not skills:
                report.add(
                    name=check_name,
                    status="warn",
                    detail=(
                        f"server {sid!r}: {path} exists but contains no "
                        "SKILL.md files."
                    ),
                    remediation=(
                        "Drop one or more `SKILL.md` files into the "
                        "directory, or run `mcp-semantic-gateway synth` "
                        "to generate them."
                    ),
                )
            else:
                report.add(
                    name=check_name,
                    status="pass",
                    detail=f"server {sid!r}: {len(skills)} SKILL.md found under {path}.",
                )


def _check_route_metadata(
    report: DoctorReport, config: MCPSemanticGatewayConfig
) -> None:
    """Stage 6: every indexed openapi tool has route_metadata populated."""

    home = gateway_home()
    metadata_db = home / "index" / "metadata.db"
    if not metadata_db.exists():
        # Already reported by _check_index_present; skip silently.
        return

    openapi_server_ids = {
        sid
        for sid, sc in config.servers.items()
        if sc.type == SourceType.OPENAPI and sc.enabled
    }
    if not openapi_server_ids:
        return

    placeholders = ",".join("?" for _ in openapi_server_ids)
    try:
        with sqlite3.connect(metadata_db) as db:
            cur = db.execute(
                f"SELECT server_id, name, route_metadata FROM tools "
                f"WHERE item_type = 'tool' AND server_id IN ({placeholders})",
                tuple(openapi_server_ids),
            )
            rows = cur.fetchall()
    except sqlite3.DatabaseError as e:
        report.add(
            name="route-metadata",
            status="fail",
            detail=f"Cannot query {metadata_db}: {e}",
        )
        return

    missing: List[str] = []
    for server_id, name, rm in rows:
        if not rm:
            missing.append(f"{server_id}::{name}")

    if not rows:
        report.add(
            name="route-metadata",
            status="warn",
            detail=(
                "No OpenAPI tools indexed yet for "
                f"{sorted(openapi_server_ids)}."
            ),
            remediation=(
                "Run `mcp-semantic-gateway index` so the forge harvests "
                "operations from your OpenAPI specs."
            ),
        )
    elif missing:
        sample = ", ".join(missing[:3])
        more = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
        report.add(
            name="route-metadata",
            status="fail",
            detail=(
                f"{len(missing)} OpenAPI tool(s) lack route_metadata: "
                f"{sample}{more}."
            ),
            remediation=(
                "Re-run `mcp-semantic-gateway index`. If the index was "
                "built by an older version, the forge may not have "
                "captured route metadata; rebuilding with the current "
                "version fixes this."
            ),
        )
    else:
        report.add(
            name="route-metadata",
            status="pass",
            detail=f"{len(rows)} OpenAPI tool(s); all have route_metadata.",
        )


# ---------------------------------------------------------------------------
# Orchestration + rendering
# ---------------------------------------------------------------------------


_STATUS_STYLES = {
    "pass": ("[green]✓[/green]", "green"),
    "fail": ("[red]✗[/red]", "red"),
    "warn": ("[yellow]![/yellow]", "yellow"),
    "skip": ("[dim]·[/dim]", "dim"),
}


def _render_table(console: Console, report: DoctorReport) -> None:
    table = Table(
        title="mcp-semantic-gateway doctor",
        show_lines=False,
        title_style="bold",
    )
    table.add_column("", justify="center", no_wrap=True)
    table.add_column("Check", style="bold")
    table.add_column("Detail", overflow="fold")
    table.add_column("Remediation", overflow="fold", style="cyan")

    for r in report.results:
        marker, _ = _STATUS_STYLES[r.status]
        table.add_row(
            marker, r.name, r.detail, r.remediation or ""
        )
    console.print(table)


async def _run(skip_network: bool, as_json: bool) -> int:
    report = DoctorReport()
    config = _check_config_loadable(report)
    if config is not None:
        _check_index_present(report, config)
        _check_auth_env_vars(report, config)
        await _check_openapi_reachable(report, config, skip_network)
        _check_skill_paths(report, config)
        _check_route_metadata(report, config)

    if as_json:
        typer.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        console = Console()
        _render_table(console, report)
        failures = report.failure_count()
        if failures:
            console.print(
                f"\n[red]✗ {failures} check(s) failed.[/red] "
                "Address the remediation column above and re-run "
                "[bold]mcp-semantic-gateway doctor[/bold]."
            )
        else:
            console.print(
                "\n[green]✓ All checks passed.[/green] "
                "Connect your agent and start querying."
            )
    return report.failure_count()


def doctor_command(
    skip_network: bool = typer.Option(
        False,
        "--no-network",
        help="Skip network checks (OpenAPI spec reachability).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """Validate gateway configuration and runtime state.

    Exits with the number of failed checks (0 on success). Designed to
    run before starting the proxy or in CI to catch misconfiguration
    early with actionable remediation.
    """

    failures = asyncio.run(_run(skip_network, as_json))
    if failures:
        # Exit code carries the failure count so CI / scripts can act on it.
        # POSIX exit codes are 8-bit unsigned; clamp at 255 in case of an
        # absurdly broken config so we don't wrap to 0.
        raise typer.Exit(code=min(failures, 255))
