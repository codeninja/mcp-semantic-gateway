"""CLI commands for use-case synthesis (Phase E).

Implements:

* ``mcp-semantic-gateway synth`` — run mining over opted-in servers.
* ``mcp-semantic-gateway synth --server <id>`` — restrict to one server.
* ``mcp-semantic-gateway synth --dry-run`` — chunk + log, no LLM calls.
* ``mcp-semantic-gateway synth status`` — list use cases per source.
* ``mcp-semantic-gateway synth --config-overlay <path>`` — layer a TOML
  file (e.g. ``.env.openai``) on top of the base config so users can
  swap providers without editing ``~/.mcp_semantic_gateway/config.toml``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import typer
import yaml

from mcp_semantic_gateway.config.loader import DEFAULT_CONFIG_PATH, load_config
from mcp_semantic_gateway.config.models import (
    MCPSemanticGatewayConfig,
    ServerConfig,
    SourceType,
)
from mcp_semantic_gateway.ingestion.collector import Collector, MCPClient
from mcp_semantic_gateway.ingestion.forge import ForgeEngine
from mcp_semantic_gateway.ingestion.observability import EventEmitter, Stage, SynthesisRun
from mcp_semantic_gateway.ingestion.use_case_miner import (
    UseCaseMiner,
    UseCaseMinerConfig,
)
from mcp_semantic_gateway.llm.factory import build_llm
from mcp_semantic_gateway.storage.init import initialize_data_dir
from mcp_semantic_gateway.storage.metadata_db import MetadataDB

synth_app = typer.Typer(name="synth", help="Use-case mining + skill synthesis")


def _resolve_config(
    config_overlay: Optional[Path],
) -> MCPSemanticGatewayConfig:
    overlays = [config_overlay] if config_overlay else None
    return load_config(overlays=overlays)


def _opted_in_servers(
    cfg: MCPSemanticGatewayConfig,
    only: Optional[str],
) -> dict[str, ServerConfig]:
    out: dict[str, ServerConfig] = {}
    for sid, sc in cfg.servers.items():
        if not sc.enabled:
            continue
        if not sc.generate_skills:
            continue
        if only and sid != only:
            continue
        out[sid] = sc
    return out


async def _harvest_for_server(
    server_id: str, sc: ServerConfig
) -> tuple[list[dict], dict | None, str]:
    """Return (harvested_tools, raw_spec_or_none, source_kind)."""

    if sc.type == SourceType.OPENAPI:
        if not sc.url:
            raise ValueError(f"server {server_id} has no url")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(sc.url)
            resp.raise_for_status()
            text = resp.text
        try:
            spec = json.loads(text)
        except json.JSONDecodeError:
            spec = yaml.safe_load(text)
        tools = ForgeEngine.forge_tools(spec)
        return tools, spec, "openapi"
    if sc.type == SourceType.MCP:
        client = MCPClient(server_id, sc)
        await client.start()
        try:
            tools = await client.call_tools_list()
        finally:
            await client.stop()
        return tools, None, "mcp"
    raise ValueError(
        f"server {server_id} type={sc.type} is not synthable (use openapi or mcp)"
    )


async def _run_mining(
    cfg: MCPSemanticGatewayConfig,
    servers: dict[str, ServerConfig],
    dry_run: bool,
    progress: bool,
) -> int:
    """Mine use cases for each opted-in server. Returns exit code."""

    if cfg.llm is None and not dry_run:
        typer.echo(
            "[error] [llm] config block missing. Configure a provider or copy "
            "an overlay (e.g. mcp-semantic-gateway synth --config-overlay .env.openai)",
            err=True,
        )
        return 2

    base_dir = initialize_data_dir()
    db = MetadataDB(base_dir / "index" / "metadata.db")
    await db.initialize()

    log_path = base_dir / "logs" / "synthesis.jsonl"
    diagnostics_dir = base_dir / "diagnostics" / "synthesis"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    run = SynthesisRun.new(server_ids=list(servers.keys()))

    if dry_run:
        llm = None
        model_id = "(dry-run)"
    else:
        llm = build_llm(cfg.llm)
        model_id = cfg.llm.model

    miner_cfg = UseCaseMinerConfig(
        model_id=model_id,
        prompt_version=cfg.skill_generation.prompt_version,
        chunk_size=cfg.skill_generation.chunk_size,
        max_concurrency=cfg.llm.max_concurrency if cfg.llm else 4,
    )

    with EventEmitter(
        run=run,
        log_path=log_path,
        diagnostics_dir=diagnostics_dir,
        progress=progress,
    ) as emitter:
        for sid, sc in servers.items():
            try:
                harvested, spec, kind = await _harvest_for_server(sid, sc)
            except Exception as exc:  # pragma: no cover (network)
                emitter.emit(
                    Stage.MINING_FAILED,
                    server_id=sid,
                    error=f"harvest failed: {exc!r}",
                )
                continue

            if dry_run:
                # Still build chunks and emit chunk events so users can
                # see what would have been mined.
                from mcp_semantic_gateway.ingestion.chunker import (
                    chunk_tools,
                    compute_source_hash_mcp,
                    compute_source_hash_openapi,
                )

                source_hash = (
                    compute_source_hash_openapi(spec)
                    if kind == "openapi" and spec is not None
                    else compute_source_hash_mcp(harvested)
                )
                chunks = chunk_tools(
                    harvested,
                    server_id=sid,
                    source_hash=source_hash,
                    source_kind=kind,  # type: ignore[arg-type]
                    chunk_size=cfg.skill_generation.chunk_size,
                )
                emitter.emit(
                    Stage.MINING_STARTED,
                    server_id=sid,
                    total_chunks=len(chunks),
                    total_tools=len(harvested),
                    dry_run=True,
                )
                for chunk in chunks:
                    emitter.emit(
                        Stage.CHUNK_STARTED,
                        server_id=sid,
                        chunk_id=chunk.chunk_id,
                        group_label=chunk.group_label,
                        tool_count=len(chunk.tools),
                        dry_run=True,
                    )
                    emitter.emit(
                        Stage.CHUNK_COMPLETED,
                        server_id=sid,
                        chunk_id=chunk.chunk_id,
                        group_label=chunk.group_label,
                        tool_count=len(chunk.tools),
                        use_cases_emitted=0,
                        dry_run=True,
                    )
                emitter.emit(
                    Stage.MINING_COMPLETED,
                    server_id=sid,
                    total_use_cases=0,
                    duration_ms=0,
                    dry_run=True,
                )
                continue

            assert llm is not None
            miner = UseCaseMiner(
                llm=llm,
                db=db,
                emitter=emitter,
                config=miner_cfg,
            )
            try:
                await miner.mine_source(
                    server_id=sid,
                    harvested_tools=harvested,
                    source_kind=kind,  # type: ignore[arg-type]
                    spec=spec,
                )
            except Exception as exc:
                emitter.emit(
                    Stage.MINING_FAILED,
                    server_id=sid,
                    error=repr(exc),
                )

        typer.echo("\n" + emitter.render_summary())
    return 0


def _format_dt(s: str) -> str:
    try:
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s


@synth_app.callback(invoke_without_command=True)
def synth_main(
    ctx: typer.Context,
    server: Optional[str] = typer.Option(None, "--server", help="Restrict to one server id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Chunk and log without LLM calls"),
    config_overlay: Optional[Path] = typer.Option(
        None,
        "--config-overlay",
        help="TOML file layered on top of the base config (e.g., .env.openai)",
    ),
    no_progress: bool = typer.Option(False, "--no-progress", help="Disable Rich progress UI"),
):
    """Run use-case mining over opted-in servers."""

    if ctx.invoked_subcommand is not None:
        # The subcommand will rebuild config itself; stash the overlay so it
        # can be picked up.
        ctx.ensure_object(dict)
        ctx.obj["config_overlay"] = config_overlay
        return

    cfg = _resolve_config(config_overlay)
    servers = _opted_in_servers(cfg, only=server)
    if not servers:
        typer.echo(
            "No opted-in servers found. Set generate_skills=true on at least one "
            "server in your config (and ensure it's enabled).",
            err=True,
        )
        raise typer.Exit(code=1)

    code = asyncio.run(_run_mining(cfg, servers, dry_run=dry_run, progress=not no_progress))
    if code != 0:
        raise typer.Exit(code=code)


@synth_app.command("status")
def synth_status(
    ctx: typer.Context,
    config_overlay: Optional[Path] = typer.Option(
        None,
        "--config-overlay",
        help="TOML file layered on top of the base config",
    ),
):
    """List use cases per source with last-run timestamp and counts."""

    overlay = config_overlay
    if overlay is None and ctx.obj:
        overlay = ctx.obj.get("config_overlay")

    cfg = _resolve_config(overlay)

    async def _go() -> None:
        base_dir = initialize_data_dir()
        db = MetadataDB(base_dir / "index" / "metadata.db")
        await db.initialize()
        rows = await db.list_use_cases()
        if not rows:
            typer.echo("No use cases mined yet.")
            return
        from collections import defaultdict

        by_source: dict[tuple[str, str], list] = defaultdict(list)
        for r in rows:
            by_source[(r.server_id, r.source_hash)].append(r)
        typer.echo(
            f"{'server_id':<24} {'source_hash':<16} {'count':>6} {'last_generated_at':>20}"
        )
        typer.echo("-" * 70)
        for (sid, sh), recs in sorted(by_source.items()):
            last = max(r.generated_at for r in recs)
            typer.echo(
                f"{sid:<24} {sh[:14] + '..':<16} {len(recs):>6} "
                f"{_format_dt(last.isoformat()):>20}"
            )

    asyncio.run(_go())
